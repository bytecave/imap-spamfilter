"""IMAP spam filter.

Hard rules enforced in this file:
  * Never EXPUNGE, never set \\Deleted, never IMAP DELETE.
  * "Remove" means IMAP MOVE to another folder. Trash retention is the
    mail provider's responsibility.
  * Fail closed: on any uncertainty (rspamd unreachable, parse error,
    folder missing) keep messages in their current folder.
  * No autolearn from rspamd scoring. Bayes learns only from explicit
    user moves (or the Train-Spam folder).
"""

from __future__ import annotations

import email
import email.policy
import logging
import os
import re
import signal
import sqlite3
import ssl
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from email.utils import getaddresses, parseaddr
from pathlib import Path
from typing import Any, Iterator

import requests
import yaml
from imapclient import IMAPClient
from imapclient.exceptions import IMAPClientError

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", "/app/accounts.yml"))
STATE_DIR = Path(os.environ.get("STATE_DIR", "/state"))
DB_PATH = STATE_DIR / "spamfilter.db"
HEARTBEAT_PATH = STATE_DIR / "heartbeat"

RSPAMD_SCAN_URL = os.environ.get("RSPAMD_SCAN_URL", "http://spamfilter-rspamd:11333/checkv2")
RSPAMD_LEARN_URL = os.environ.get("RSPAMD_LEARN_URL", "http://spamfilter-rspamd:11334")


def _load_rspamd_password() -> str:
    val = os.environ.get("RSPAMD_PASSWORD", "").strip()
    if val:
        return val
    # Fallback: shared appdata file written by the Unraid bootstrap script.
    pwfile = Path(os.environ.get("STATE_DIR", "/state")) / "controller.password"
    if pwfile.is_file():
        return pwfile.read_text().strip()
    return ""


RSPAMD_PASSWORD = _load_rspamd_password()
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

# Optional env overrides for accounts.yml `defaults` (Unraid template uses these
# so the user can set retention windows without editing YAML). Unset = ignore.
_ENV_DEFAULT_OVERRIDES: dict[str, str] = {
    "junk_retention_days": "DEFAULT_JUNK_RETENTION_DAYS",
    "trained_retention_days": "DEFAULT_TRAINED_RETENTION_DAYS",
}

FLIP_FLOP_COOLDOWN_S = 600       # 10 min between opposite learns for one msg
UNLEARNABLE_RETRY_S = 30 * 86400 # retry messages marked 'unlearnable' after 30d
SAFE_MODE_UNSEEN_CAP = 500       # refuse to process if Inbox unseen > this
SCAN_FETCH_CHUNK = 50            # max msgs fetched per scan_inbox FETCH call
RECONNECT_MIN_BACKOFF = 5
RECONNECT_MAX_BACKOFF = 300
HTTP_TIMEOUT = 30
MAX_FETCH_BYTES = 5 * 1024 * 1024

JUNK_KEYWORD = "$Junk"           # RFC 5788 - verify with your client
NOTJUNK_KEYWORD = "$NotJunk"

VALID_MODES = {"shadow", "flag", "move"}

SHUTDOWN = threading.Event()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass
class Account:
    name: str
    user: str
    password: str

    imap_host: str
    imap_port: int
    ssl: bool

    inbox: str
    junk: str
    trash: str
    spam_train: str
    trained_spam: str
    ham_train: str
    trained_ham: str

    mode: str
    threshold: float
    min_threshold_allowed: float
    reject_score_above: float

    move_grace_seconds: int
    learn_grace_seconds: int
    idle_timeout: int
    poll_interval: int
    junk_poll_interval: int
    retention_check_interval: int

    max_moves_per_hour: int
    max_learns_per_hour: int
    max_train_per_run: int

    junk_retention_days: int
    trained_retention_days: int

    learn_from_moves: bool
    auto_special_folders: bool

    # Optional rspamd Bayes identity. If unset, falls back to acc.user so
    # each IMAP account has its own per-recipient Bayes data. Set the same
    # value across multiple accounts to pool their Bayes training.
    bayes_user: str | None = None

    # Populated at runtime once IMAP delimiter is known.
    delimiter: str = "/"
    folder_map: dict[str, str] = field(default_factory=dict)


# Built-in defaults applied unless overridden in accounts.yml `defaults:` or in
# a per-account block. Users can leave `defaults:` empty (or omit it entirely)
# and only specify per-account credentials. Required-from-the-user keys (name,
# user, password, imap_host) are NOT here on purpose - they have no sensible
# default and the loader raises if missing.
BUILTIN_DEFAULTS: dict[str, Any] = {
    "imap_port": 993,
    "ssl": True,
    "inbox": "INBOX",
    "junk": "Junk",
    "trash": "Trash",
    # All four training folders live under the server's junk parent so
    # they are auto-relocated together when SPECIAL-USE remaps the junk
    # name (e.g. "Junk/" -> "Spam/" on Dovecot \Junk-flagged accounts).
    "spam_train": "Junk/Train-Spam",
    "trained_spam": "Junk/Trained-Spam",
    "ham_train": "Junk/Train-Ham",
    "trained_ham": "Junk/Trained-Ham",
    "mode": "shadow",
    "threshold": 8.0,
    "min_threshold_allowed": 5.0,
    "reject_score_above": 100.0,
    "move_grace_seconds": 60,
    "learn_grace_seconds": 300,
    "idle_timeout": 1500,
    "poll_interval": 600,
    "junk_poll_interval": 120,
    "retention_check_interval": 3600,
    "max_moves_per_hour": 30,
    "max_learns_per_hour": 50,
    "max_train_per_run": 100,
    "junk_retention_days": 10,
    "trained_retention_days": 7,
    "learn_from_moves": True,
    # Optional. If unset (None), per-account Bayes is keyed by acc.user.
    "bayes_user": None,
    # When True, look up junk/trash folders via RFC 6154 SPECIAL-USE flags
    # on connect and override the configured names if the server advertises
    # them. Lets Apple Mail / Thunderbird / Outlook users keep their client-
    # chosen folder name (e.g. "Spam") without editing accounts.yml.
    "auto_special_folders": True,
}

REQUIRED_PER_ACCOUNT: tuple[str, ...] = ("name", "user", "password", "imap_host")


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in overlay.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _apply_env_overrides(defaults: dict[str, Any]) -> dict[str, Any]:
    out = dict(defaults)
    for cfg_key, env_key in _ENV_DEFAULT_OVERRIDES.items():
        val = os.environ.get(env_key)
        if val is None or val == "":
            continue
        try:
            out[cfg_key] = int(val)
        except ValueError:
            raise SystemExit(f"env {env_key}={val!r}: not an integer")
    return out


def _clean_bayes_user(raw: Any, account_name: str) -> str | None:
    """Validate the bayes_user override. Rejects CR/LF so the value cannot
    be smuggled as an extra header line via the `Delivered-To:` prefix we
    prepend to /learn{spam,ham} bodies."""
    if not raw:
        return None
    s = str(raw)
    if "\r" in s or "\n" in s:
        raise SystemExit(
            f"account {account_name!r}: bayes_user must not contain CR or LF"
        )
    return s


def load_accounts(path: Path) -> list[Account]:
    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, dict) or "accounts" not in raw:
        raise SystemExit(f"{path}: missing 'accounts' key")
    # Built-ins, then user defaults, then env overrides.
    defaults = _apply_env_overrides(_deep_merge(BUILTIN_DEFAULTS, raw.get("defaults") or {}))
    out: list[Account] = []
    seen: set[str] = set()
    for entry in raw["accounts"]:
        if not isinstance(entry, dict):
            raise SystemExit(f"{path}: each account must be a mapping")
        merged = _deep_merge(defaults, entry)
        missing = [k for k in REQUIRED_PER_ACCOUNT if not merged.get(k)]
        if missing:
            raise SystemExit(
                f"{path}: account {entry.get('name', '?')!r} missing required key(s): "
                f"{', '.join(missing)}"
            )
        try:
            acc = Account(
                name=merged["name"],
                user=merged["user"],
                password=merged["password"],
                imap_host=merged["imap_host"],
                imap_port=int(merged["imap_port"]),
                ssl=bool(merged["ssl"]),
                inbox=merged["inbox"],
                junk=merged["junk"],
                trash=merged["trash"],
                spam_train=merged["spam_train"],
                trained_spam=merged["trained_spam"],
                ham_train=merged["ham_train"],
                trained_ham=merged["trained_ham"],
                mode=str(merged["mode"]),
                threshold=float(merged["threshold"]),
                min_threshold_allowed=float(merged["min_threshold_allowed"]),
                reject_score_above=float(merged["reject_score_above"]),
                move_grace_seconds=int(merged["move_grace_seconds"]),
                learn_grace_seconds=int(merged["learn_grace_seconds"]),
                idle_timeout=int(merged["idle_timeout"]),
                poll_interval=int(merged["poll_interval"]),
                junk_poll_interval=int(merged["junk_poll_interval"]),
                retention_check_interval=int(merged["retention_check_interval"]),
                max_moves_per_hour=int(merged["max_moves_per_hour"]),
                max_learns_per_hour=int(merged["max_learns_per_hour"]),
                max_train_per_run=int(merged["max_train_per_run"]),
                junk_retention_days=int(merged["junk_retention_days"]),
                trained_retention_days=int(merged["trained_retention_days"]),
                learn_from_moves=bool(merged["learn_from_moves"]),
                auto_special_folders=bool(merged["auto_special_folders"]),
                bayes_user=_clean_bayes_user(merged.get("bayes_user"), merged.get("name", "?")),
            )
        except KeyError as ex:
            raise SystemExit(f"account {entry.get('name')!r}: missing config key {ex.args[0]!r}") from ex
        if acc.name in seen:
            raise SystemExit(f"duplicate account name: {acc.name}")
        seen.add(acc.name)
        validate_account(acc)
        out.append(acc)
    if not out:
        raise SystemExit("no accounts configured")
    return out


def validate_account(acc: Account) -> None:
    if acc.mode not in VALID_MODES:
        raise SystemExit(f"{acc.name}: invalid mode {acc.mode!r}")
    # acc.user is the bayes_user fallback when no explicit bayes_user is
    # configured, and the value ends up in a Delivered-To header we
    # inject into the rspamd /learn body. Reject CR/LF here for the same
    # reason _clean_bayes_user does, otherwise an operator who pastes a
    # multi-line value into accounts.yml smuggles arbitrary headers.
    for field_name in ("user", "name"):
        val = str(getattr(acc, field_name))
        if "\r" in val or "\n" in val:
            raise SystemExit(
                f"{acc.name}: account {field_name} must not contain CR or LF"
            )
    if not 1 <= acc.imap_port <= 65535:
        raise SystemExit(f"{acc.name}: imap_port out of range ({acc.imap_port})")
    if acc.min_threshold_allowed <= 0:
        raise SystemExit(
            f"{acc.name}: min_threshold_allowed must be positive "
            f"({acc.min_threshold_allowed})"
        )
    if acc.threshold < acc.min_threshold_allowed:
        raise SystemExit(
            f"{acc.name}: threshold {acc.threshold} below min_threshold_allowed "
            f"{acc.min_threshold_allowed}"
        )
    if acc.reject_score_above < acc.threshold:
        raise SystemExit(
            f"{acc.name}: reject_score_above {acc.reject_score_above} must be "
            f">= threshold {acc.threshold} (otherwise every legit score is "
            f"discarded as out-of-range)"
        )
    folder_set = {
        acc.inbox, acc.junk, acc.trash,
        acc.spam_train, acc.trained_spam,
        acc.ham_train, acc.trained_ham,
    }
    if len(folder_set) != 7:
        raise SystemExit(
            f"{acc.name}: inbox/junk/trash/spam_train/trained_spam/"
            f"ham_train/trained_ham must all be distinct"
        )
    if not 1 <= acc.max_moves_per_hour <= 1000:
        raise SystemExit(f"{acc.name}: max_moves_per_hour out of range")
    if not 1 <= acc.max_learns_per_hour <= 1000:
        raise SystemExit(f"{acc.name}: max_learns_per_hour out of range")
    if not 1 <= acc.max_train_per_run <= 5000:
        raise SystemExit(f"{acc.name}: max_train_per_run out of range")
    if acc.learn_grace_seconds < 0 or acc.move_grace_seconds < 0:
        raise SystemExit(f"{acc.name}: grace values must be >= 0")
    if acc.junk_retention_days < 0 or acc.trained_retention_days < 0:
        raise SystemExit(f"{acc.name}: retention days must be >= 0")


# ---------------------------------------------------------------------------
# SQLite
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    account            TEXT NOT NULL,
    message_id         TEXT NOT NULL,
    first_seen         INTEGER NOT NULL,
    last_seen          INTEGER NOT NULL,
    current_folder     TEXT NOT NULL,
    moved_to_junk_at   INTEGER,
    our_score          REAL,
    our_action         TEXT,
    learned_as         TEXT,
    learned_at         INTEGER,
    pending_learn      TEXT,         -- 'spam' | 'ham' | NULL
    pending_learn_at   INTEGER,
    sender             TEXT,
    subject            TEXT,
    received_at        INTEGER,      -- IMAP INTERNALDATE (unix); NULL if unknown
    PRIMARY KEY (account, message_id)
);

CREATE TABLE IF NOT EXISTS uidvalidity (
    account     TEXT NOT NULL,
    folder      TEXT NOT NULL,
    uidvalidity INTEGER NOT NULL,
    PRIMARY KEY (account, folder)
);

CREATE TABLE IF NOT EXISTS pending_move (
    account     TEXT NOT NULL,
    uidvalidity INTEGER NOT NULL,
    uid         INTEGER NOT NULL,
    message_id  TEXT NOT NULL,
    flag_at     INTEGER NOT NULL,
    PRIMARY KEY (account, uidvalidity, uid)
);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    account     TEXT NOT NULL,
    ts          INTEGER NOT NULL,
    message_id  TEXT,
    event       TEXT NOT NULL,
    detail      TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_time ON events(account, ts);

CREATE TABLE IF NOT EXISTS rate_limit (
    account TEXT NOT NULL,
    action  TEXT NOT NULL,
    ts      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rate_limit ON rate_limit(account, action, ts);

CREATE TABLE IF NOT EXISTS safe_mode (
    account     TEXT NOT NULL,
    scope       TEXT NOT NULL,
    entered_at  INTEGER NOT NULL,
    reason      TEXT NOT NULL,
    PRIMARY KEY (account, scope)
);
"""


def init_db() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript("PRAGMA journal_mode=WAL;")
        conn.executescript(SCHEMA)
        _migrate(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after the original schema. CREATE TABLE IF
    NOT EXISTS never alters an existing table, so new columns need an
    explicit ADD COLUMN on databases created before they were added."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(messages)")}
    if "received_at" not in cols:
        conn.execute("ALTER TABLE messages ADD COLUMN received_at INTEGER")


class Db:
    """Per-thread SQLite handle. Use as context manager for write transactions."""

    def __init__(self, account: str) -> None:
        self.account = account
        self.conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=30000")

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        self.conn.execute("BEGIN")
        try:
            yield self.conn
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    # ----- events -----------------------------------------------------------

    def log_event(self, event: str, message_id: str | None = None, detail: str | None = None) -> None:
        self.conn.execute(
            "INSERT INTO events(account, ts, message_id, event, detail) VALUES(?,?,?,?,?)",
            (self.account, int(time.time()), message_id, event, detail),
        )

    # ----- messages ---------------------------------------------------------

    def get_message(self, msgid: str) -> sqlite3.Row | None:
        cur = self.conn.execute(
            "SELECT * FROM messages WHERE account=? AND message_id=?",
            (self.account, msgid),
        )
        return cur.fetchone()

    def upsert_message(
        self,
        msgid: str,
        folder: str,
        sender: str,
        subject: str,
        received_at: int | None = None,
    ) -> None:
        now = int(time.time())
        self.conn.execute(
            """
            INSERT INTO messages(account, message_id, first_seen, last_seen,
                                 current_folder, sender, subject, received_at)
            VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(account, message_id) DO UPDATE SET
                last_seen=excluded.last_seen,
                current_folder=excluded.current_folder,
                sender=COALESCE(messages.sender, excluded.sender),
                subject=COALESCE(messages.subject, excluded.subject),
                received_at=COALESCE(messages.received_at, excluded.received_at)
            """,
            (self.account, msgid, now, now, folder, sender, subject, received_at),
        )

    # Whitelist of message-table columns the rest of the filter is
    # allowed to update through update_message(). update_message builds
    # its SET clause from kwarg names; without this guard a typo or a
    # future caller could put an arbitrary identifier into the SQL.
    _UPDATABLE_MESSAGE_COLUMNS = frozenset({
        "current_folder", "moved_to_junk_at", "our_score", "our_action",
        "learned_as", "learned_at", "pending_learn", "pending_learn_at",
        "sender", "subject",
    })

    def update_message(self, msgid: str, **fields: Any) -> None:
        if not fields:
            return
        bad = set(fields) - self._UPDATABLE_MESSAGE_COLUMNS
        if bad:
            raise ValueError(f"update_message: unknown column(s) {sorted(bad)}")
        cols = ", ".join(f"{k}=?" for k in fields)
        vals = list(fields.values()) + [self.account, msgid]
        self.conn.execute(
            f"UPDATE messages SET {cols} WHERE account=? AND message_id=?",
            vals,
        )

    # ----- uidvalidity ------------------------------------------------------

    def get_uidvalidity(self, folder: str) -> int | None:
        cur = self.conn.execute(
            "SELECT uidvalidity FROM uidvalidity WHERE account=? AND folder=?",
            (self.account, folder),
        )
        row = cur.fetchone()
        return row[0] if row else None

    def set_uidvalidity(self, folder: str, value: int) -> None:
        self.conn.execute(
            """
            INSERT INTO uidvalidity(account, folder, uidvalidity) VALUES(?,?,?)
            ON CONFLICT(account, folder) DO UPDATE SET uidvalidity=excluded.uidvalidity
            """,
            (self.account, folder, value),
        )

    # ----- pending_move (flag->move grace, filter-initiated only) ----------

    def add_pending_move(self, uidvalidity: int, uid: int, msgid: str) -> None:
        self.conn.execute(
            """
            INSERT OR IGNORE INTO pending_move(account, uidvalidity, uid, message_id, flag_at)
            VALUES(?,?,?,?,?)
            """,
            (self.account, uidvalidity, uid, msgid, int(time.time())),
        )

    def due_pending_moves(self, uidvalidity: int, grace_s: int) -> list[sqlite3.Row]:
        cutoff = int(time.time()) - grace_s
        cur = self.conn.execute(
            """
            SELECT uid, message_id, flag_at FROM pending_move
            WHERE account=? AND uidvalidity=? AND flag_at<=?
            ORDER BY flag_at
            """,
            (self.account, uidvalidity, cutoff),
        )
        return list(cur.fetchall())

    def drop_pending_move(self, uidvalidity: int, uid: int) -> None:
        self.conn.execute(
            "DELETE FROM pending_move WHERE account=? AND uidvalidity=? AND uid=?",
            (self.account, uidvalidity, uid),
        )

    # ----- rate limiting ----------------------------------------------------

    def record_rate(self, action: str) -> None:
        self.conn.execute(
            "INSERT INTO rate_limit(account, action, ts) VALUES(?,?,?)",
            (self.account, action, int(time.time())),
        )

    def rate_count(self, action: str, window_s: int) -> int:
        since = int(time.time()) - window_s
        cur = self.conn.execute(
            "SELECT COUNT(*) FROM rate_limit WHERE account=? AND action=? AND ts>=?",
            (self.account, action, since),
        )
        return int(cur.fetchone()[0])

    def prune_rate(self, older_than_s: int = 7200) -> None:
        cutoff = int(time.time()) - older_than_s
        self.conn.execute(
            "DELETE FROM rate_limit WHERE account=? AND ts<?",
            (self.account, cutoff),
        )

    def prune_events(self, older_than_s: int = 30 * 86400) -> int:
        """Drop event rows older than the cutoff. Keeps the DB bounded
        over 24/7/365 operation - the events table is debug/forensic
        only and accumulates dozens of rows per account per hour."""
        cutoff = int(time.time()) - older_than_s
        cur = self.conn.execute(
            "DELETE FROM events WHERE account=? AND ts<?",
            (self.account, cutoff),
        )
        return cur.rowcount

    def prune_messages(self, older_than_s: int) -> int:
        """Drop fully-resolved message rows older than the cutoff. A row
        is fully resolved when there is no pending_learn (we are not
        waiting on a grace timer) and either it has been moved out of
        Inbox or it was scored long enough ago that we will not revisit
        the decision."""
        cutoff = int(time.time()) - older_than_s
        cur = self.conn.execute(
            """
            DELETE FROM messages
             WHERE account=?
               AND last_seen<?
               AND pending_learn IS NULL
            """,
            (self.account, cutoff),
        )
        return cur.rowcount

    def prune_stale_pending_learn(self, older_than_s: int) -> int:
        """Clear pending_learn flags that have been sitting past their
        grace window without resolving. Without this, a message that
        was scheduled for a grace-window learn but then disappeared
        from its folder (user moved it, server resynced UIDs, etc.)
        keeps its pending_learn forever and prevents prune_messages
        from ever evicting the row.

        Cleared rows can then be picked up by prune_messages on the
        next sweep if they are also old enough by last_seen."""
        cutoff = int(time.time()) - older_than_s
        cur = self.conn.execute(
            """
            UPDATE messages
               SET pending_learn=NULL, pending_learn_at=NULL
             WHERE account=?
               AND pending_learn IS NOT NULL
               AND pending_learn_at<?
            """,
            (self.account, cutoff),
        )
        return cur.rowcount

    def vacuum_if_due(self, every_s: int = 7 * 86400) -> None:
        """Run an incremental SQLite VACUUM at most once per week to
        reclaim space from the periodic prunes above. Cheap on a few-MB
        DB, prevents WAL growth over a multi-year run."""
        cur = self.conn.execute("PRAGMA user_version").fetchone()
        last = int(cur[0]) if cur else 0
        now = int(time.time())
        if last and now - last < every_s:
            return
        # Stamp the marker FIRST so an interrupted VACUUM (process kill,
        # disk full mid-rewrite) does not cause a tight retry loop where
        # every retention sweep tries VACUUM again and fails the same way.
        self.conn.execute(f"PRAGMA user_version = {now}")
        try:
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self.conn.execute("VACUUM")
        except sqlite3.Error as ex:
            # Surface VACUUM failure: the marker is now set so we will not
            # retry for a week, which is the right tradeoff for spin-loop
            # avoidance but the wrong tradeoff to fail silently. Log loudly
            # and write an event so the operator notices DB growth before
            # the next vacuum window rolls around.
            logging.getLogger("main").error(
                "vacuum failed for %s; next attempt in %dd: %s",
                self.account, every_s // 86400, ex,
            )
            try:
                self.log_event("vacuum_failed", detail=str(ex)[:300])
            except sqlite3.Error:
                pass

    # ----- safe mode --------------------------------------------------------

    def in_safe_mode(self, scope: str) -> bool:
        cur = self.conn.execute(
            "SELECT 1 FROM safe_mode WHERE account=? AND scope IN (?, 'all')",
            (self.account, scope),
        )
        return cur.fetchone() is not None

    def enter_safe_mode(self, scope: str, reason: str) -> None:
        self.conn.execute(
            """
            INSERT OR REPLACE INTO safe_mode(account, scope, entered_at, reason)
            VALUES(?,?,?,?)
            """,
            (self.account, scope, int(time.time()), reason),
        )

    def exit_safe_mode(self, scope: str) -> None:
        self.conn.execute(
            "DELETE FROM safe_mode WHERE account=? AND scope=?",
            (self.account, scope),
        )


# ---------------------------------------------------------------------------
# rspamd
# ---------------------------------------------------------------------------


def rspamd_scan(
    raw: bytes, recipient: str, max_score: float, bayes_user: str | None = None
) -> float | None:
    """POST to /checkv2. Return numeric score or None on any error.

    `bayes_user`, if given, is used as the `Rcpt` header so rspamd's
    per-user classifier (with `users_enabled = true`) looks up Bayes data
    under that identity instead of the message recipient. Multiple accounts
    using the same `bayes_user` share a Bayes namespace.
    """
    try:
        rcpt = bayes_user or recipient
        headers = {"Rcpt": rcpt, "From": recipient}
        resp = requests.post(RSPAMD_SCAN_URL, data=raw, headers=headers, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        score = data.get("score")
        if not isinstance(score, (int, float)):
            return None
        score = float(score)
        if score < -max_score or score > max_score:
            return None
        return score
    except (requests.RequestException, ValueError):
        return None


def rspamd_learn(raw: bytes, kind: str, user: str) -> str:
    """POST to /learnspam or /learnham; classify the controller's reply.

    Returns one of:
      * 'learned'  - HTTP 200, a fresh learn committed to Bayes;
      * 'already'  - HTTP 208, the message was already learned;
      * 'declined' - HTTP 204: rspamd processed the request and learned
        nothing (too few tokens, or the message is already in that
        class). Re-POSTing the identical bytes always yields the same
        result, so the caller must treat this as terminal, not retry it;
      * 'error'    - a network failure or any other status (5xx, or a
        4xx such as a wrong controller password). Transient or operator-
        fixable; the caller may retry, subject to its own cap.

    `user` becomes the classifier user for per-user Bayes
    (`users_enabled = true`) by prepending a `Delivered-To: <user>` header
    to the message bytes before POSTing. Rspamd reads that header to pick
    the classifier namespace. The HTTP `User` header is ignored by
    rspamd's controller for classifier identity, so message-header
    injection is the only working path. Must match the recipient used at
    scan time for the data to apply on future deliveries.
    """
    assert kind in {"spam", "ham"}
    url = f"{RSPAMD_LEARN_URL}/learn{kind}"
    headers = {"Password": RSPAMD_PASSWORD}
    body = f"Delivered-To: {user}\r\n".encode() + raw
    try:
        resp = requests.post(url, data=body, headers=headers, timeout=HTTP_TIMEOUT)
    except requests.RequestException as ex:
        logging.getLogger("filter").warning("rspamd learn POST failed: %s", ex)
        return "error"
    if resp.status_code == 200:
        return "learned"
    if resp.status_code == 208:
        return "already"
    if resp.status_code == 204:
        return "declined"
    # Any other status: log it so an unexpected reply (or a misconfigured
    # controller password) is visible, and let the caller retry.
    logging.getLogger("filter").warning(
        "rspamd learn(%s) unexpected HTTP %s: %s",
        kind, resp.status_code, (resp.text or "").strip()[:200])
    return "error"


# ---------------------------------------------------------------------------
# IMAP helpers
# ---------------------------------------------------------------------------


def detect_delimiter(client: IMAPClient) -> str:
    try:
        folders = client.list_folders()
    except IMAPClientError:
        return "/"
    for _flags, delim, _name in folders:
        if delim:
            return delim if isinstance(delim, str) else delim.decode("ascii", "replace")
    return "/"


def resolve_folder(name: str, delim: str) -> str:
    if delim == "/" or "/" not in name:
        return name
    return name.replace("/", delim)


def build_folder_map(acc: Account, delim: str) -> dict[str, str]:
    return {
        "inbox": resolve_folder(acc.inbox, delim),
        "junk": resolve_folder(acc.junk, delim),
        "trash": resolve_folder(acc.trash, delim),
        "spam_train": resolve_folder(acc.spam_train, delim),
        "trained_spam": resolve_folder(acc.trained_spam, delim),
        "ham_train": resolve_folder(acc.ham_train, delim),
        "trained_ham": resolve_folder(acc.trained_ham, delim),
    }


def detect_special_folders(client: IMAPClient) -> dict[str, str]:
    """Return {'junk': name, 'trash': name} for folders carrying RFC 6154
    SPECIAL-USE flags. Empty dict if the server doesn't advertise them.
    """
    flag_map = {r"\Junk": "junk", r"\Trash": "trash"}
    out: dict[str, str] = {}
    for flags, _delim, name in client.list_folders():
        for f in flags or ():
            s = f.decode("ascii", "replace") if isinstance(f, bytes) else str(f)
            key = flag_map.get(s)
            if key and key not in out:
                out[key] = name if isinstance(name, str) else name.decode("utf-8", "replace")
    return out


def ensure_folders(client: IMAPClient, log: logging.Logger, fmap: dict[str, str]) -> None:
    # Don't trust LIST alone: some servers return placeholders (\Noselect /
    # \NonExistent) for parents whose children we created earlier, which
    # confuses a name-only existence check. Probe with SELECT instead.
    #
    # Policy: refuse to create core user folders (inbox, junk, trash). If
    # they don't already exist the operator has either picked the wrong
    # account or misconfigured a name; auto-creating them silently can
    # lead to two parallel "Junk" / "Spam" hierarchies and chaos in the
    # user's mailbox (we hit this exact problem during initial setup).
    # We only auto-create the filter-specific training folders.
    REQUIRED = ("inbox", "junk", "trash")
    AUTO_CREATE = ("spam_train", "trained_spam", "ham_train", "trained_ham")

    missing: list[str] = []
    for key in REQUIRED:
        try:
            client.select_folder(fmap[key], readonly=True)
        except IMAPClientError:
            missing.append(f"{key}={fmap[key]!r}")
    if missing:
        raise RuntimeError(
            "required folder(s) missing on the IMAP server: "
            + ", ".join(missing)
            + ". The filter refuses to create core mailbox folders to avoid "
            "creating duplicate junk/trash hierarchies. Either enable "
            "auto_special_folders so SPECIAL-USE flags are honoured, or set "
            "explicit junk/trash names in accounts.yml that match folders "
            "that already exist on the server."
        )

    for key in AUTO_CREATE:
        f = fmap[key]
        try:
            client.select_folder(f, readonly=True)
            continue  # exists
        except IMAPClientError:
            pass
        log.info("creating missing folder %s", f)
        try:
            client.create_folder(f)
            client.subscribe_folder(f)
        except IMAPClientError as ex:
            log.warning("create_folder(%s) failed: %s", f, ex)


def select_with_uidvalidity_check(
    client: IMAPClient, db: Db, folder: str, log: logging.Logger, readonly: bool = False
) -> int:
    info = client.select_folder(folder, readonly=readonly)
    uv = int(info[b"UIDVALIDITY"])
    stored = db.get_uidvalidity(folder)
    if stored is not None and stored != uv:
        log.warning("UIDVALIDITY changed for %s: %d -> %d, resyncing", folder, stored, uv)
        db.log_event("uidvalidity_change", detail=f"{folder} {stored}->{uv}")
        with db.tx():
            db.conn.execute(
                "DELETE FROM pending_move WHERE account=? AND uidvalidity=?",
                (db.account, stored),
            )
            # pending_learn rows for messages that lived in this folder are
            # also unsafe: the UIDs they would have been promoted under no
            # longer exist, and Message-ID search may resolve to a stale or
            # missing message. Drop pending_learn so the next user move (or
            # next drain pass for spam_train) re-creates a clean row.
            cancelled = db.conn.execute(
                """
                UPDATE messages SET pending_learn=NULL, pending_learn_at=NULL
                 WHERE account=? AND current_folder=? AND pending_learn IS NOT NULL
                """,
                (db.account, folder),
            ).rowcount
            if cancelled:
                db.log_event(
                    "pending_canceled_uidvalidity",
                    detail=f"{folder} cleared {cancelled} pending_learn rows",
                )
    with db.tx():
        db.set_uidvalidity(folder, uv)
    return uv


def parse_envelope(raw: bytes) -> tuple[str | None, str, str]:
    """Return (message_id, subject, sender_address). Never raises."""
    try:
        msg = email.message_from_bytes(raw, policy=email.policy.compat32)
        msgid = msg.get("Message-ID", "").strip()
        if msgid:
            m = re.search(r"<([^>]+)>", msgid)
            if m:
                msgid = m.group(1)
        subject = str(msg.get("Subject", "") or "")[:500]
        sender = parseaddr(str(msg.get("From", "") or ""))[1][:200]
        return (msgid or None, subject, sender)
    except Exception:
        return (None, "", "")


def _internaldate_ts(data: dict) -> int | None:
    """Convert an IMAP FETCH INTERNALDATE value to a unix timestamp.
    IMAPClient yields it as a datetime; return None when absent or unparseable."""
    dt = data.get(b"INTERNALDATE")
    if dt is None:
        return None
    try:
        return int(dt.timestamp())
    except (AttributeError, OverflowError, OSError, ValueError):
        return None


def first_recipient(raw: bytes, fallback: str) -> str:
    try:
        msg = email.message_from_bytes(raw, policy=email.policy.compat32)
        addrs = getaddresses(msg.get_all("To", []) + msg.get_all("Cc", []))
        for _name, addr in addrs:
            if addr:
                return addr
    except Exception:
        pass
    return fallback


def fetch_chunked(
    client: IMAPClient, uids: list[int], items: list[bytes]
) -> Iterator[tuple[int, dict]]:
    """Yield (uid, data) pairs, FETCHing `uids` in SCAN_FETCH_CHUNK-sized
    batches. A whole-folder FETCH of message bodies can otherwise hold
    every body in memory at once; chunking caps peak use at one batch."""
    for start in range(0, len(uids), SCAN_FETCH_CHUNK):
        batch = client.fetch(uids[start : start + SCAN_FETCH_CHUNK], items)
        yield from batch.items()


# ---------------------------------------------------------------------------
# Per-account worker
# ---------------------------------------------------------------------------


@dataclass
class AccountState:
    last_junk_poll: float = 0.0
    last_retention: float = 0.0
    scan_fail_streak: int = 0


def _kw(flags: tuple[bytes, ...] | list[bytes]) -> set[str]:
    out: set[str] = set()
    for f in flags or ():
        if isinstance(f, bytes):
            out.add(f.decode("ascii", "replace"))
        else:
            out.add(str(f))
    return out


_HEARTBEAT_LOCK = threading.Lock()


def heartbeat() -> None:
    # Account threads and the main watchdog all call heartbeat()
    # concurrently. Without serialisation two threads racing on the
    # same .tmp path corrupt each other's writes and leave one of the
    # tmp.replace() calls failing with FileNotFoundError. The critical
    # section is two filesystem ops; contention is negligible.
    with _HEARTBEAT_LOCK:
        try:
            # Atomic write so a partial / interrupted heartbeat write
            # does not leave a truncated file that healthcheck reads
            # as "0" (unix epoch = stale by ~50 years). On NFS the
            # rename is atomic per POSIX.
            tmp = HEARTBEAT_PATH.with_suffix(".tmp")
            tmp.write_text(str(int(time.time())))
            tmp.replace(HEARTBEAT_PATH)
        except OSError as ex:
            # Surface the failure so the operator notices a stuck NFS
            # mount or disk full before the healthcheck staleness
            # threshold trips. Use logger directly to avoid recursion
            # if logging itself blocks on the same fs.
            logging.getLogger("main").warning("heartbeat write failed: %s", ex)


_RATE_LOG_LOCK = threading.Lock()
_RATE_LOG_LAST: dict[tuple[str, str], float] = {}


def check_rate(db: Db, log: logging.Logger, action: str, limit: int) -> bool:
    """True if allowed, False if rate-limit hit. Soft refusal only -
    the rate window rolls off naturally, no sticky safe-mode entry.
    The sliding rate_limit table is already self-bounded to 1 hour."""
    count = db.rate_count(action, 3600)
    if count >= limit:
        # Log once per minute at most to avoid log spam during a burst.
        # The last-log map is shared across account threads so it must
        # be guarded; CPython dict ops are atomic individually but the
        # read-then-write pattern here is not.
        key = (db.account, action)
        now = time.time()
        with _RATE_LOG_LOCK:
            last = _RATE_LOG_LAST.get(key, 0.0)
            should_log = (now - last) > 60
            if should_log:
                _RATE_LOG_LAST[key] = now
        if should_log:
            log.warning("%s rate limit hit (%d/%d per hour), refusing for now",
                        action, count, limit)
            db.log_event("rate_limited", detail=f"{action} {count}/{limit}")
        return False
    return True


def try_learn(
    db: Db, log: logging.Logger, acc: Account, raw: bytes, msgid: str, kind: str, reason: str
) -> bool:
    if not acc.learn_from_moves:
        return False
    if db.in_safe_mode("learning"):
        log.warning("skip learn (%s) for %s: in safe mode", kind, msgid)
        return False
    row = db.get_message(msgid)
    if row is not None:
        learned = row["learned_as"]
        if learned == kind:
            # Already learned as same kind. Skip rspamd POST (would return
            # 208 and burn a rate slot) but report success so the caller
            # (e.g. drain_train_spam) still moves the message out.
            return True
        if learned == "unlearnable":
            # rspamd previously refused this content's tokens (typically
            # min_tokens not met). Retry once a month in case the body
            # changed (some IMAP servers allow APPEND+flag edits) and so
            # operators do not need to manually clear the row to retry.
            last = row["learned_at"] or 0
            if int(time.time()) - last < UNLEARNABLE_RETRY_S:
                # Report success so caller still moves the message out
                # of the train folder without endlessly re-asking rspamd.
                return True
            # Old enough to retry: fall through to the rspamd_learn call.
        if learned and learned != kind:
            last = row["learned_at"] or 0
            if int(time.time()) - last < FLIP_FLOP_COOLDOWN_S:
                log.warning("skip learn (%s) for %s: flip-flop cooldown active", kind, msgid)
                db.log_event("learn_flipflop_block", msgid, detail=f"{learned}->{kind}")
                return False
    if not check_rate(db, log, "learn", acc.max_learns_per_hour):
        return False
    outcome = rspamd_learn(raw, kind, user=acc.bayes_user or acc.user)
    if outcome == "error":
        log.warning("rspamd learn(%s) failed for %s", kind, msgid)
        db.log_event("learn_failed", msgid, detail=kind)
        return False
    if outcome == "declined":
        # rspamd processed the message and deliberately learned nothing
        # (too few tokens, or already in that class). Retrying the same
        # bytes is futile: mark it so try_learn short-circuits future
        # calls, and report success so the caller moves it out.
        log.info("rspamd declined to learn %s as %s", msgid, kind)
        with db.tx():
            db.update_message(msgid, learned_as="unlearnable",
                              learned_at=int(time.time()),
                              pending_learn=None, pending_learn_at=None)
            db.log_event("learn_skipped", msgid, detail=kind)
        return True
    # 'learned' or 'already': the message is in the desired Bayes class.
    now = int(time.time())
    with db.tx():
        db.update_message(msgid, learned_as=kind, learned_at=now, pending_learn=None, pending_learn_at=None)
        db.record_rate("learn")
        db.log_event(f"learn_{kind}", msgid, detail=reason)
    log.info("learned %s as %s (%s)", msgid, kind, reason)
    return True


# ----- scan / poll / drain -------------------------------------------------


def scan_inbox(
    client: IMAPClient,
    db: Db,
    log: logging.Logger,
    acc: Account,
    fmap: dict[str, str],
    state: "AccountState | None" = None,
) -> None:
    uv = select_with_uidvalidity_check(client, db, fmap["inbox"], log)
    unseen = client.search(["UNSEEN"])
    all_uids = client.search(["ALL"])  # also detect Junk->Inbox reverts that aren't unseen
    candidates = sorted(set(unseen) | set(all_uids[-200:] if all_uids else []))
    if len(unseen) > SAFE_MODE_UNSEEN_CAP:
        reason = f"Inbox UNSEEN > {SAFE_MODE_UNSEEN_CAP} ({len(unseen)}) - refusing to process"
        log.error(reason)
        with db.tx():
            db.enter_safe_mode("all", reason)
            db.log_event("safe_mode", detail=reason)
        return
    # Auto-exit safe mode once the trigger condition (unseen > cap) clears.
    # The only enter_safe_mode("all", ...) call site is the cap check above,
    # so observing unseen <= cap is sufficient to know it is safe to resume.
    if db.in_safe_mode("all"):
        log.info("exiting safe mode (unseen=%d back under cap=%d)",
                 len(unseen), SAFE_MODE_UNSEEN_CAP)
        with db.tx():
            db.exit_safe_mode("all")
            db.log_event("safe_mode_exit", detail=f"unseen={len(unseen)}")

    if not candidates:
        return

    # Chunk the FETCH so a single oversized inbox does not allocate up
    # to len(candidates) * MAX_FETCH_BYTES at once. With 200 candidates
    # and 5 MB cap that would be 1 GB of resident memory.
    for chunk_start in range(0, len(candidates), SCAN_FETCH_CHUNK):
        if SHUTDOWN.is_set():
            return
        chunk = candidates[chunk_start : chunk_start + SCAN_FETCH_CHUNK]
        fetched = client.fetch(chunk, [b"BODY.PEEK[]", b"FLAGS", b"INTERNALDATE"])
        for uid, data in fetched.items():
            if SHUTDOWN.is_set():
                return
            raw = data.get(b"BODY[]") or data.get(b"BODY.PEEK[]")
            if not raw:
                continue
            flags = _kw(data.get(b"FLAGS", ()))
            msgid, subject, sender = parse_envelope(raw)
            if not msgid:
                log.debug("inbox uid %s: no Message-ID, skipping", uid)
                continue

            prior = db.get_message(msgid)
            prior_folder = prior["current_folder"] if prior else None
            with db.tx():
                db.upsert_message(msgid, fmap["inbox"], sender, subject,
                                  _internaldate_ts(data))

            # Detect Junk -> Inbox revert (user moved a message we
            # previously placed in Junk, or one that lived in Junk, back
            # to Inbox). An IMAP move preserves the \Seen flag, so a
            # reverted message the user already read in Junk arrives
            # *seen*; gating this on `uid in unseen` would silently drop
            # the most common ham signal. prior_folder flips to inbox
            # after the upsert above, so this branch fires only once.
            reverted = prior_folder == fmap["junk"]
            if reverted:
                if NOTJUNK_KEYWORD in flags:
                    # User intent confirmed by $NotJunk keyword skips grace.
                    try_learn(db, log, acc, raw, msgid, "ham", reason="revert+notjunk_kw")
                elif prior["learned_as"] != "ham" and prior["pending_learn"] != "ham":
                    # Schedule ham learn after grace.
                    with db.tx():
                        db.update_message(msgid, pending_learn="ham", pending_learn_at=int(time.time()))
                        db.log_event("pending_ham", msgid, detail="user revert")
                continue

            # Skip scoring on already-scored messages (avoid duplicate moves).
            if prior is not None and prior["our_score"] is not None:
                continue
            # Only score unseen messages on first appearance.
            if uid not in unseen:
                continue

            recipient = first_recipient(raw, acc.user)
            score = rspamd_scan(
                raw, recipient, acc.reject_score_above, bayes_user=acc.bayes_user or acc.user
            )
            if score is None:
                log.warning("scan failed for %s (uid=%s) - keeping in inbox", msgid, uid)
                db.log_event("scan_failed", msgid)
                if state is not None:
                    state.scan_fail_streak += 1
                    # Escalate loudly at 1/10/50/250+ so rspamd downtime
                    # shows up in logs and Unraid notification scrapers
                    # without spamming once per scanned message.
                    if state.scan_fail_streak in (1, 10, 50) or state.scan_fail_streak % 250 == 0:
                        log.error(
                            "rspamd scan failing: %d consecutive failures (check rspamd container)",
                            state.scan_fail_streak,
                        )
                        db.log_event("scan_fail_streak", detail=str(state.scan_fail_streak))
                continue
            if state is not None and state.scan_fail_streak:
                log.info("rspamd scan recovered after %d failures", state.scan_fail_streak)
                db.log_event("scan_recovered", detail=str(state.scan_fail_streak))
                state.scan_fail_streak = 0

            with db.tx():
                db.update_message(msgid, our_score=score)
                db.log_event("scan", msgid, detail=f"score={score:.2f} mode={acc.mode}")
            log.debug("scored %s = %.2f", msgid, score)

            if score < acc.threshold:
                continue

            # Over threshold: act according to mode.
            match acc.mode:
                case "shadow":
                    log.info("[shadow] would flag %s score=%.2f subj=%r", msgid, score, subject[:80])
                    with db.tx():
                        db.update_message(msgid, our_action="shadow")
                case "flag":
                    client.add_flags(uid, [b"\\Flagged"])
                    log.info("[flag] flagged %s score=%.2f", msgid, score)
                    with db.tx():
                        db.update_message(msgid, our_action="flagged")
                        db.log_event("flagged", msgid, detail=f"score={score:.2f}")
                case "move":
                    # Do NOT set \Flagged: that is the user's "starred"
                    # flag and we pollute it once per spam during the
                    # grace window. Record the pending_move with just a
                    # DB row + event - the move happens after
                    # move_grace_seconds whether or not the flag is set.
                    with db.tx():
                        db.add_pending_move(uv, uid, msgid)
                        db.update_message(msgid, our_action="pending_move")
                        db.log_event("pending_move", msgid, detail=f"score={score:.2f}")


def execute_due_moves(client: IMAPClient, db: Db, log: logging.Logger, acc: Account, fmap: dict[str, str]) -> None:
    if acc.mode != "move":
        return
    if db.in_safe_mode("all"):
        return
    uv = select_with_uidvalidity_check(client, db, fmap["inbox"], log)
    rows = db.due_pending_moves(uv, acc.move_grace_seconds)
    if not rows:
        return
    if not check_rate(db, log,"move", acc.max_moves_per_hour):
        return

    uids = [r["uid"] for r in rows]
    fetched = client.fetch(uids, [b"FLAGS"])
    to_move: list[int] = []
    for r in rows:
        uid = r["uid"]
        if uid not in fetched:
            with db.tx():
                db.drop_pending_move(uv, uid)
                db.log_event("move_skipped_missing", r["message_id"])
            continue
        to_move.append(uid)

    if not to_move:
        return

    # Cap this batch to the hourly quota still available. check_rate
    # above only confirms at least one slot is free; without subtracting
    # the moves already recorded this hour, a single batch could move a
    # further max_moves_per_hour on top of them.
    remaining = acc.max_moves_per_hour - db.rate_count("move", 3600)
    to_move = to_move[: max(0, remaining)]
    if not to_move:
        return
    try:
        client.move(to_move, fmap["junk"])
    except IMAPClientError as ex:
        log.warning("move to junk failed: %s", ex)
        db.log_event("move_failed", detail=str(ex)[:200])
        return

    now = int(time.time())
    with db.tx():
        for r in rows:
            if r["uid"] not in to_move:
                continue
            db.drop_pending_move(uv, r["uid"])
            db.update_message(
                r["message_id"],
                current_folder=fmap["junk"],
                our_action="moved_to_junk",
                moved_to_junk_at=now,
            )
            db.record_rate("move")
            db.log_event("moved_to_junk", r["message_id"])
    log.info("moved %d message(s) inbox->junk", len(to_move))


def poll_junk(client: IMAPClient, db: Db, log: logging.Logger, acc: Account, fmap: dict[str, str]) -> None:
    select_with_uidvalidity_check(client, db, fmap["junk"], log)
    uids = client.search(["ALL"])
    if not uids:
        # Still process any time-due pending learns from DB even with no junk content.
        process_pending_learns(client, db, log, acc, fmap)
        return
    # Chunk the FETCH so a very large Junk folder does not pull every
    # message body into memory at once (same rationale as scan_inbox).
    for chunk_start in range(0, len(uids), SCAN_FETCH_CHUNK):
        if SHUTDOWN.is_set():
            return
        chunk = uids[chunk_start : chunk_start + SCAN_FETCH_CHUNK]
        fetched = client.fetch(chunk, [b"BODY.PEEK[]", b"FLAGS", b"INTERNALDATE"])
        for uid, data in fetched.items():
            if SHUTDOWN.is_set():
                return
            raw = data.get(b"BODY[]") or data.get(b"BODY.PEEK[]")
            if not raw:
                continue
            flags = _kw(data.get(b"FLAGS", ()))
            msgid, subject, sender = parse_envelope(raw)
            if not msgid:
                continue
            prior = db.get_message(msgid)
            prior_folder = prior["current_folder"] if prior else None
            with db.tx():
                db.upsert_message(msgid, fmap["junk"], sender, subject,
                                  _internaldate_ts(data))

            # Filter put it here: nothing to learn. Skip.
            if prior is not None and prior["our_action"] == "moved_to_junk":
                continue
            # Learn spam only from a confirmed user move Inbox -> Junk,
            # i.e. we have a prior row showing the message was in Inbox.
            # Mail with no prior row (delivered straight to Junk by the
            # provider's own filter, never seen in Inbox) is NOT an
            # explicit user move and is deliberately not learned - that
            # would train Bayes on the provider's verdict, including its
            # false positives.
            if prior_folder == fmap["inbox"]:
                if JUNK_KEYWORD in flags:
                    try_learn(db, log, acc, raw, msgid, "spam", reason="user_move+junk_kw")
                else:
                    with db.tx():
                        if not prior or prior["pending_learn"] != "spam":
                            db.update_message(
                                msgid,
                                pending_learn="spam",
                                pending_learn_at=int(time.time()),
                                moved_to_junk_at=int(time.time()),
                            )
                            db.log_event("pending_spam", msgid, detail="user move inbox->junk")

    process_pending_learns(client, db, log, acc, fmap)


def process_pending_learns(
    client: IMAPClient, db: Db, log: logging.Logger, acc: Account, fmap: dict[str, str]
) -> None:
    """Promote pending spam/ham learns that have outlived the grace window."""
    cutoff = int(time.time()) - acc.learn_grace_seconds
    cur = db.conn.execute(
        """
        SELECT message_id, pending_learn, current_folder FROM messages
        WHERE account=? AND pending_learn IS NOT NULL AND pending_learn_at<=?
        """,
        (db.account, cutoff),
    )
    candidates = list(cur.fetchall())
    if not candidates:
        return

    # Fetch raw bodies one-by-one (each may be in its current folder).
    folder_groups: dict[str, list[sqlite3.Row]] = {}
    for row in candidates:
        folder_groups.setdefault(row["current_folder"], []).append(row)

    for folder, rows in folder_groups.items():
        # Reconfirm folder still expected for the kind.
        try:
            client.select_folder(folder)
        except IMAPClientError as ex:
            log.warning("select %s for pending learn failed: %s", folder, ex)
            continue
        for r in rows:
            if SHUTDOWN.is_set():
                return
            msgid = r["message_id"]
            kind = r["pending_learn"]
            # Spam pending must still be in Junk; ham pending must still be in Inbox.
            if kind == "spam" and folder != fmap["junk"]:
                with db.tx():
                    db.update_message(msgid, pending_learn=None, pending_learn_at=None)
                    db.log_event("pending_canceled", msgid, detail="moved before grace")
                continue
            if kind == "ham" and folder != fmap["inbox"]:
                with db.tx():
                    db.update_message(msgid, pending_learn=None, pending_learn_at=None)
                    db.log_event("pending_canceled", msgid, detail="moved before grace")
                continue
            # Locate UID via HEADER Message-ID search.
            uids = client.search(["HEADER", "Message-ID", msgid])
            if not uids:
                with db.tx():
                    db.update_message(msgid, pending_learn=None, pending_learn_at=None)
                    db.log_event("pending_lost", msgid, detail=f"not found in {folder}")
                continue
            uid = uids[0]
            data = client.fetch([uid], [b"BODY.PEEK[]"])
            raw = data.get(uid, {}).get(b"BODY[]") or data.get(uid, {}).get(b"BODY.PEEK[]")
            if not raw:
                continue
            if try_learn(db, log, acc, raw, msgid, kind, reason="grace_elapsed"):
                continue
            # try_learn returned False: a transient rspamd error left the
            # pending_learn flag set. Without a cap the same message is
            # re-POSTed every poll until the stale-pending sweep clears it
            # (up to 24h). Give up after 3 failures, mirroring
            # _drain_train_folder.
            fails = db.conn.execute(
                "SELECT COUNT(*) FROM events WHERE account=? AND "
                "message_id=? AND event='learn_failed'",
                (db.account, msgid),
            ).fetchone()[0]
            if fails >= 3:
                with db.tx():
                    db.update_message(
                        msgid, learned_as="unlearnable",
                        learned_at=int(time.time()),
                        pending_learn=None, pending_learn_at=None)
                    db.log_event("learn_giveup", msgid,
                                 detail=f"{kind} after {fails} failures")
                log.warning("pending learn: giving up on %s after %d "
                            "failures", msgid, fails)


def _drain_train_folder(
    client: IMAPClient,
    db: Db,
    log: logging.Logger,
    acc: Account,
    fmap: dict[str, str],
    *,
    kind: str,
    src_key: str,
    dst_key: str,
) -> None:
    """Generic 'pull mail out of a train folder, learn it under `kind`,
    move to the trained folder' loop. kind is 'spam' or 'ham'; src_key
    and dst_key index into fmap (e.g. 'spam_train' -> 'trained_spam'
    or 'ham_train' -> 'trained_ham')."""
    assert kind in {"spam", "ham"}
    if not acc.learn_from_moves:
        return
    folder = fmap[src_key]
    try:
        uv = select_with_uidvalidity_check(client, db, folder, log)
    except IMAPClientError as ex:
        log.warning("select %s failed: %s", folder, ex)
        return
    uids = client.search(["ALL"])
    if not uids:
        return
    uids = uids[: acc.max_train_per_run]
    learned_uids: list[int] = []
    moved_msgids: list[str] = []
    log_tag = f"drain_train_{kind}"
    reason = f"train_{kind}_folder"
    for uid, data in fetch_chunked(client, uids, [b"BODY.PEEK[]", b"INTERNALDATE"]):
        if SHUTDOWN.is_set():
            return
        raw = data.get(b"BODY[]") or data.get(b"BODY.PEEK[]")
        if not raw:
            continue
        msgid, subject, sender = parse_envelope(raw)
        if not msgid:
            # No Message-ID header. Skip DB tracking entirely - we
            # cannot safely identify the row across folder moves later
            # (the synthetic uv+uid id is no longer addressable once
            # the message is moved under a different uidvalidity), so
            # storing it would just leak stale rows that retention
            # can never update. Use a synthetic per-pass id for event
            # logging only, and let rspamd dedup by content hash.
            msgid = f"<no-msgid-uv-{uv}-uid-{uid}>"
            synthetic = True
        else:
            synthetic = False
            with db.tx():
                db.upsert_message(msgid, folder, sender, subject,
                                  _internaldate_ts(data))
        ok = try_learn(db, log, acc, raw, msgid, kind, reason=reason)
        if ok:
            learned_uids.append(uid)
            if not synthetic:
                moved_msgids.append(msgid)
            continue
        # try_learn returned False: rspamd refused (min_tokens not met,
        # encoded body, etc.) or transient. Count prior learn_failed
        # events for this msgid; after 3 fails, give up so the train
        # folder cannot loop forever on the same UID.
        fails = db.conn.execute(
            "SELECT COUNT(*) FROM events "
            "WHERE account=? AND message_id=? AND event='learn_failed'",
            (db.account, msgid),
        ).fetchone()[0]
        if fails >= 3:
            log.warning(
                "%s: giving up on %s after %d learn failures, moving out unlearned",
                log_tag, msgid, fails,
            )
            with db.tx():
                if not synthetic:
                    db.update_message(msgid, learned_as="unlearnable", learned_at=int(time.time()))
                db.log_event("learn_giveup", msgid, detail=f"{kind} after {fails} failures")
            learned_uids.append(uid)
            if not synthetic:
                moved_msgids.append(msgid)
    if not learned_uids:
        return
    try:
        client.move(learned_uids, fmap[dst_key])
    except IMAPClientError as ex:
        log.warning("move %s -> %s failed: %s", folder, fmap[dst_key], ex)
        return
    try:
        with db.tx():
            for uid in learned_uids:
                db.log_event("trained_moved", detail=f"uid={uid} -> {fmap[dst_key]}")
            if moved_msgids:
                placeholders = ",".join("?" * len(moved_msgids))
                db.conn.execute(
                    f"UPDATE messages SET current_folder=? WHERE account=? "
                    f"AND message_id IN ({placeholders})",
                    (fmap[dst_key], db.account, *moved_msgids),
                )
    except sqlite3.Error as ex:
        log.error(
            "%s: IMAP moved %d msgs to %s but DB update failed: %s",
            log_tag, len(learned_uids), fmap[dst_key], ex,
        )
        return
    log.info("%s: learned+moved %d", log_tag, len(learned_uids))


def drain_train_spam(
    client: IMAPClient, db: Db, log: logging.Logger, acc: Account, fmap: dict[str, str]
) -> None:
    _drain_train_folder(
        client, db, log, acc, fmap,
        kind="spam", src_key="spam_train", dst_key="trained_spam",
    )


def drain_train_ham(
    client: IMAPClient, db: Db, log: logging.Logger, acc: Account, fmap: dict[str, str]
) -> None:
    _drain_train_folder(
        client, db, log, acc, fmap,
        kind="ham", src_key="ham_train", dst_key="trained_ham",
    )


# ----- retention ----------------------------------------------------------


def retention_sweep(
    client: IMAPClient, db: Db, log: logging.Logger, acc: Account, fmap: dict[str, str]
) -> None:
    if acc.junk_retention_days > 0:
        _sweep_folder_to_trash(
            client, db, log, acc, fmap,
            src=fmap["junk"], days=acc.junk_retention_days, exclude_learned_ham=True, tag="junk_retention",
        )
    if acc.trained_retention_days > 0:
        _sweep_folder_to_trash(
            client, db, log, acc, fmap,
            src=fmap["trained_spam"], days=acc.trained_retention_days, exclude_learned_ham=False, tag="trained_retention",
        )
        _sweep_folder_to_trash(
            client, db, log, acc, fmap,
            src=fmap["trained_ham"], days=acc.trained_retention_days, exclude_learned_ham=False, tag="trained_ham_retention",
        )


def _sweep_folder_to_trash(
    client: IMAPClient, db: Db, log: logging.Logger, acc: Account, fmap: dict[str, str],
    *, src: str, days: int, exclude_learned_ham: bool, tag: str,
) -> None:
    if db.in_safe_mode("all"):
        return
    try:
        client.select_folder(src)
    except IMAPClientError as ex:
        log.warning("retention: select %s failed: %s", src, ex)
        return
    # IMAP `BEFORE <date>` is interpreted by the SERVER in its local
    # timezone (RFC 3501 4.3 - dates have no time, server compares
    # against INTERNALDATE in server-local time). Subtract one extra
    # day so a TZ mismatch between the container clock and the
    # server's clock cannot cause us to delete messages slightly too
    # early. Worst case: retention runs one day late, never one day
    # early.
    cutoff_date = time.strftime(
        "%d-%b-%Y",
        time.gmtime(time.time() - (days + 1) * 86400),
    )
    try:
        uids = client.search(["BEFORE", cutoff_date])
    except IMAPClientError as ex:
        log.warning("retention: search BEFORE %s failed: %s", cutoff_date, ex)
        return
    if not uids:
        return
    uids = uids[:500]
    to_move: list[int] = []
    moved_msgids: list[str] = []
    # Always fetch Message-ID so we can update current_folder in the DB
    # after the move; otherwise rows referencing src would persist
    # forever and a future re-train would silent-skip them.
    fetched = client.fetch(uids, [b"BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)]"])
    for uid in uids:
        raw = (
            fetched.get(uid, {}).get(b"BODY[HEADER.FIELDS (MESSAGE-ID)]")
            or fetched.get(uid, {}).get(b"BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)]")
            or b""
        )
        msgid, _, _ = parse_envelope(raw)
        if exclude_learned_ham and msgid:
            row = db.get_message(msgid)
            if row is not None and row["learned_as"] == "ham":
                continue
        to_move.append(uid)
        if msgid:
            moved_msgids.append(msgid)
        else:
            # The DB row (if any) is keyed by Message-ID; without one we
            # cannot address it, so the corresponding row stays stale
            # until prune_messages eventually drops it. The IMAP move
            # itself is still safe. Surface the rare case for visibility.
            log.debug(
                "retention %s: uid=%s in %s has no Message-ID; "
                "moving without DB current_folder update",
                tag, uid, src,
            )
    if not to_move:
        return
    try:
        client.move(to_move, fmap["trash"])
    except IMAPClientError as ex:
        log.warning("retention: move %s -> %s failed: %s", src, fmap["trash"], ex)
        return
    try:
        with db.tx():
            db.log_event(tag, detail=f"moved {len(to_move)} from {src} to {fmap['trash']}")
            if moved_msgids:
                placeholders = ",".join("?" * len(moved_msgids))
                db.conn.execute(
                    f"UPDATE messages SET current_folder=? WHERE account=? "
                    f"AND message_id IN ({placeholders})",
                    (fmap["trash"], db.account, *moved_msgids),
                )
    except sqlite3.Error as ex:
        log.error(
            "retention sweep %s: IMAP moved %d msgs to %s but DB update failed: %s",
            tag, len(to_move), fmap["trash"], ex,
        )
        return
    log.info("retention sweep %s: moved %d %s -> %s", tag, len(to_move), src, fmap["trash"])


# ----- main loop ----------------------------------------------------------


def account_loop(acc: Account) -> None:
    """Per-account thread entry point. Owns the thread's Db handle and
    closes it even if the worker raises, so a crash + watchdog restart
    does not leak a SQLite connection per cycle."""
    db = Db(acc.name)
    try:
        _run_account(acc, db)
    finally:
        db.close()
        logging.getLogger(acc.name).info("thread exiting")


def _run_account(acc: Account, db: Db) -> None:
    log = logging.getLogger(acc.name)
    threading.current_thread().name = acc.name
    state = AccountState()
    backoff = RECONNECT_MIN_BACKOFF

    while not SHUTDOWN.is_set():
        client: IMAPClient | None = None
        try:
            log.info("connecting to %s:%d as %s", acc.imap_host, acc.imap_port, acc.user)
            # Pin a strict TLS context so a future imapclient version cannot
            # silently relax the default - we want hostname verification and
            # the system CA bundle, no exceptions.
            ssl_ctx = ssl.create_default_context() if acc.ssl else None
            client = IMAPClient(
                acc.imap_host,
                port=acc.imap_port,
                ssl=acc.ssl,
                ssl_context=ssl_ctx,
                timeout=60,
            )
            client.login(acc.user, acc.password)
            delim = detect_delimiter(client)
            acc.delimiter = delim
            if acc.auto_special_folders:
                detected = detect_special_folders(client)
                old_junk = acc.junk
                for key in ("junk", "trash"):
                    name = detected.get(key)
                    if not name:
                        continue
                    current = getattr(acc, key)
                    if name != current:
                        log.info("auto-detected %s folder via SPECIAL-USE: %s (was %s)",
                                 key, name, current)
                        setattr(acc, key, name)
                # Keep spam_train/trained_spam under whatever the new junk
                # is, regardless of what prefix the operator originally
                # configured. Catches operators who don't use literal
                # "Junk/..." (e.g. "Spam/...") when the server later remaps.
                if "junk" in detected and acc.junk != old_junk:
                    new_junk = acc.junk
                    prefix = old_junk + "/"
                    for key in ("spam_train", "trained_spam", "ham_train", "trained_ham"):
                        cur = getattr(acc, key)
                        if cur.startswith(prefix):
                            remapped = new_junk + cur[len(old_junk):]
                            log.info("remapped %s: %s -> %s", key, cur, remapped)
                            setattr(acc, key, remapped)
            acc.folder_map = build_folder_map(acc, delim)
            ensure_folders(client, log, acc.folder_map)
            idle_cap = client.has_capability("IDLE")
            log.info("connected, delimiter=%r, mode=%s, IMAP IDLE=%s",
                     delim, acc.mode, "yes" if idle_cap else "no")
            if not idle_cap:
                poll_s = min(acc.idle_timeout, max(30, acc.junk_poll_interval))
                log.warning(
                    "server %s does not advertise IMAP IDLE; new mail is "
                    "detected by the %ds poll, not instant push",
                    acc.imap_host, poll_s)
            backoff = RECONNECT_MIN_BACKOFF

            while not SHUTDOWN.is_set():
                heartbeat()
                db.prune_rate()

                drain_train_spam(client, db, log, acc, acc.folder_map)
                if SHUTDOWN.is_set():
                    break
                drain_train_ham(client, db, log, acc, acc.folder_map)
                if SHUTDOWN.is_set():
                    break

                scan_inbox(client, db, log, acc, acc.folder_map, state)
                if SHUTDOWN.is_set():
                    break
                execute_due_moves(client, db, log, acc, acc.folder_map)

                now = time.monotonic()
                if now - state.last_junk_poll >= acc.junk_poll_interval:
                    poll_junk(client, db, log, acc, acc.folder_map)
                    state.last_junk_poll = now

                if now - state.last_retention >= acc.retention_check_interval:
                    retention_sweep(client, db, log, acc, acc.folder_map)
                    # Keep the local DB bounded over years of uptime.
                    # `messages` rows we no longer need for revert
                    # detection get dropped once last_seen is older than
                    # both retention windows combined; events older than
                    # 30 days are debug-only. VACUUM runs weekly inside.
                    msg_window = (
                        max(acc.junk_retention_days, acc.trained_retention_days, 14)
                        + 7
                    ) * 86400
                    # Pending_learn rows older than max(grace) * 24
                    # are stale: the grace window has long expired and
                    # process_pending_learns either resolved them or
                    # could not find the message. Clear so the row can
                    # be evicted by prune_messages on the same sweep.
                    stale_pending_window = (
                        max(acc.learn_grace_seconds, acc.move_grace_seconds, 3600) * 24
                    )
                    with db.tx():
                        cleared = db.prune_stale_pending_learn(stale_pending_window)
                        ev = db.prune_events()
                        ms = db.prune_messages(msg_window)
                    if cleared or ev or ms:
                        log.info(
                            "pruned events=%d messages=%d stale_pending=%d",
                            ev, ms, cleared,
                        )
                    db.vacuum_if_due()
                    state.last_retention = now

                # IDLE on Inbox until activity, junk_poll_interval, or idle_timeout.
                # Poll SHUTDOWN every 60s so a SIGTERM during a long IDLE wait
                # does not leave the account thread blocked for up to
                # idle_timeout (~25 min). Also keeps the IDLE chunk well under
                # any server-side IDLE cap (RFC 2177 mentions 29 min).
                client.select_folder(acc.folder_map["inbox"])
                wait = min(acc.idle_timeout, max(30, acc.junk_poll_interval))
                # 30s idle_chunk keeps us comfortably below the 60s socket
                # timeout, so a hung peer is detected within one chunk and
                # SHUTDOWN is observed within ~30s rather than ~120s.
                idle_chunk = 30
                idle_failed = False
                try:
                    client.idle()
                    elapsed = 0
                    while elapsed < wait and not SHUTDOWN.is_set():
                        step = min(idle_chunk, wait - elapsed)
                        if client.idle_check(timeout=step):
                            break  # server pushed activity, exit IDLE early
                        elapsed += step
                finally:
                    try:
                        client.idle_done()
                    except (IMAPClientError, OSError) as ex:
                        # Server likely closed the connection silently
                        # (idle timeout, restart, network blip). Force a
                        # reconnect rather than continuing on a half-dead
                        # socket; the outer except handles the error path.
                        log.warning("idle_done failed: %s; forcing reconnect", ex)
                        idle_failed = True
                if idle_failed:
                    raise IMAPClientError("idle_done failed")
        except (IMAPClientError, OSError) as ex:
            log.warning("connection error: %s (backoff %ds)", ex, backoff)
            db.log_event("conn_error", detail=str(ex)[:300])
        except Exception as ex:  # noqa: BLE001 - last-resort guard
            # Use error+truncated str rather than log.exception(): the full
            # traceback can echo the IMAP password back if the underlying
            # exception text contains it (some servers reflect the LOGIN
            # arguments in their error response).
            log.error(
                "unhandled error in account loop (%s): %s",
                type(ex).__name__, str(ex)[:300],
            )
            db.log_event("unhandled_error", detail=str(ex)[:300])
        finally:
            if client is not None:
                try:
                    client.logout()
                except Exception:
                    pass
        if SHUTDOWN.is_set():
            break
        for _ in range(backoff):
            if SHUTDOWN.is_set():
                break
            time.sleep(1)
        backoff = min(backoff * 2, RECONNECT_MAX_BACKOFF)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def configure_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL, logging.INFO),
        format="%(asctime)s [%(threadName)s] %(levelname)s %(message)s",
        stream=sys.stdout,
    )


def install_signal_handlers() -> None:
    def _handler(signum: int, _frame: Any) -> None:
        logging.getLogger("main").info("received signal %d, shutting down", signum)
        SHUTDOWN.set()
    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)


def main() -> int:
    configure_logging()
    log = logging.getLogger("main")
    if not RSPAMD_PASSWORD:
        log.error("RSPAMD_PASSWORD is unset")
        return 2
    if not CONFIG_PATH.exists():
        log.error("config not found at %s", CONFIG_PATH)
        return 2
    init_db()
    install_signal_handlers()

    # Optional read-only dashboard (Flask + waitress). Disabled unless
    # at least one dashboard user is configured - via the
    # state/dashboard_users file, the DASHBOARD_USERS env var, or the
    # legacy DASHBOARD_USER + DASHBOARD_PASSWORD pair. dashboard.start()
    # makes the final call; this gate just avoids importing Flask when
    # the dashboard is clearly unused. Listens on a fixed internal port
    # 8080; the orchestrator chooses the host port.
    if (
        os.environ.get("DASHBOARD_USERS")
        or (os.environ.get("DASHBOARD_USER")
            and os.environ.get("DASHBOARD_PASSWORD"))
        or (STATE_DIR / "dashboard_users").is_file()
    ):
        try:
            import dashboard as _dashboard
            _dashboard.start()
        except Exception as ex:  # noqa: BLE001
            log.error("failed to start dashboard: %s", ex)

    accounts = load_accounts(CONFIG_PATH)
    log.info("loaded %d account(s): %s", len(accounts), ", ".join(a.name for a in accounts))
    heartbeat()

    threads: dict[str, threading.Thread] = {}
    for acc in accounts:
        t = threading.Thread(target=account_loop, args=(acc,), name=acc.name, daemon=False)
        t.start()
        threads[acc.name] = t
    accounts_by_name = {a.name: a for a in accounts}
    # Per-account exponential backoff so a thread that crashes immediately
    # after restart does not spin the main loop or fill logs at 1 Hz.
    restart_backoff: dict[str, float] = {a.name: 0.0 for a in accounts}
    last_restart: dict[str, float] = {a.name: 0.0 for a in accounts}

    # Heartbeat watchdog: keep main thread alive, update heartbeat periodically
    # so it advances even if every account thread is stuck in IDLE. Restart
    # any account thread that has died - account_loop catches the common
    # exceptions itself, but an unhandled error (OOM, bug after a future
    # change) must not silently disable an account for the rest of the
    # 24/7 run.
    # An account that has been up for 10 minutes since its last restart
    # is considered healthy, so the next failure starts a fresh backoff
    # cycle from 5s rather than continuing whatever previous max it
    # reached. Without this, a thread that crashed once a year ago and
    # crashes again now waits 5 minutes to recover.
    backoff_reset_after = 600.0
    while not SHUTDOWN.is_set():
        heartbeat()
        now = time.monotonic()
        for name, t in list(threads.items()):
            if t.is_alive():
                if (
                    restart_backoff[name] > 0
                    and last_restart[name]
                    and (now - last_restart[name]) > backoff_reset_after
                ):
                    restart_backoff[name] = 0.0
                continue
            wait_for = max(0.0, last_restart[name] + restart_backoff[name] - now)
            if wait_for > 0:
                continue
            log.error(
                "account thread %s died; restarting (backoff was %.0fs)",
                name, restart_backoff[name],
            )
            acc = accounts_by_name[name]
            nt = threading.Thread(
                target=account_loop, args=(acc,), name=name, daemon=False
            )
            nt.start()
            threads[name] = nt
            last_restart[name] = now
            restart_backoff[name] = min(max(restart_backoff[name] * 2, 5.0), 300.0)
        SHUTDOWN.wait(timeout=60)

    # Account threads now poll SHUTDOWN every ~60s during IDLE, so a
    # 180s join leaves 3x cushion for the current chunk to finish plus a
    # logout round-trip. Without this, threads still in idle_check at
    # SIGTERM would be abandoned and Docker would have to SIGKILL them.
    for t in threads.values():
        t.join(timeout=180)
    log.info("all threads exited")
    return 0


if __name__ == "__main__":
    sys.exit(main())
