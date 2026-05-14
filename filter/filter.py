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
SAFE_MODE_UNSEEN_CAP = 500       # refuse to process if Inbox unseen > this
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
    "spam_train": "Junk/Train-Spam",
    "trained_spam": "Junk/Trained-Spam",
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
    if acc.threshold < acc.min_threshold_allowed:
        raise SystemExit(
            f"{acc.name}: threshold {acc.threshold} below min_threshold_allowed "
            f"{acc.min_threshold_allowed}"
        )
    folder_set = {acc.inbox, acc.junk, acc.trash, acc.spam_train, acc.trained_spam}
    if len(folder_set) != 5:
        raise SystemExit(f"{acc.name}: inbox/junk/trash/spam_train/trained_spam must all be distinct")
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

    def upsert_message(self, msgid: str, folder: str, sender: str, subject: str) -> None:
        now = int(time.time())
        self.conn.execute(
            """
            INSERT INTO messages(account, message_id, first_seen, last_seen, current_folder, sender, subject)
            VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(account, message_id) DO UPDATE SET
                last_seen=excluded.last_seen,
                current_folder=excluded.current_folder,
                sender=COALESCE(messages.sender, excluded.sender),
                subject=COALESCE(messages.subject, excluded.subject)
            """,
            (self.account, msgid, now, now, folder, sender, subject),
        )

    def update_message(self, msgid: str, **fields: Any) -> None:
        if not fields:
            return
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


# ---------------------------------------------------------------------------
# rspamd
# ---------------------------------------------------------------------------


def rspamd_scan(raw: bytes, recipient: str, max_score: float) -> float | None:
    """POST to /checkv2. Return numeric score or None on any error."""
    try:
        headers = {"Rcpt": recipient, "From": recipient}
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


def rspamd_learn(raw: bytes, kind: str) -> bool:
    """POST to /learnspam or /learnham. Accept 200 and 208 (already learned)."""
    assert kind in {"spam", "ham"}
    url = f"{RSPAMD_LEARN_URL}/learn{kind}"
    headers = {"Password": RSPAMD_PASSWORD}
    try:
        resp = requests.post(url, data=raw, headers=headers, timeout=HTTP_TIMEOUT)
    except requests.RequestException:
        return False
    return resp.status_code in (200, 208)


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
    try:
        client.select_folder(fmap["inbox"], readonly=True)
    except IMAPClientError as ex:
        raise RuntimeError(f"required folder missing on server: {fmap['inbox']} ({ex})") from ex

    for key in ("junk", "trash", "spam_train", "trained_spam"):
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


# ---------------------------------------------------------------------------
# Per-account worker
# ---------------------------------------------------------------------------


@dataclass
class AccountState:
    last_junk_poll: float = 0.0
    last_retention: float = 0.0


def _kw(flags: tuple[bytes, ...] | list[bytes]) -> set[str]:
    out: set[str] = set()
    for f in flags or ():
        if isinstance(f, bytes):
            out.add(f.decode("ascii", "replace"))
        else:
            out.add(str(f))
    return out


def heartbeat() -> None:
    try:
        HEARTBEAT_PATH.write_text(str(int(time.time())))
    except OSError:
        pass


def check_rate_or_safe_mode(db: Db, log: logging.Logger, action: str, limit: int) -> bool:
    """True if allowed, False if rate-limit hit. Safe-mode entered on breach."""
    count = db.rate_count(action, 3600)
    if count >= limit:
        scope = "all" if action == "move" else "learning"
        reason = f"{action} rate limit hit ({count}/{limit} per hour)"
        log.error("entering safe mode (%s): %s", scope, reason)
        with db.tx():
            db.enter_safe_mode(scope, reason)
            db.log_event("safe_mode", detail=reason)
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
    if row is not None and row["learned_as"] == kind:
        # Already learned this message as this kind. rspamd would return 208
        # but we'd burn a rate slot and never converge. Silently skip.
        return False
    if row is not None and row["learned_as"] and row["learned_as"] != kind:
        last = row["learned_at"] or 0
        if int(time.time()) - last < FLIP_FLOP_COOLDOWN_S:
            log.warning("skip learn (%s) for %s: flip-flop cooldown active", kind, msgid)
            db.log_event("learn_flipflop_block", msgid, detail=f"{row['learned_as']}->{kind}")
            return False
    if not check_rate_or_safe_mode(db, log, "learn", acc.max_learns_per_hour):
        return False
    if not rspamd_learn(raw, kind):
        log.warning("rspamd learn(%s) failed for %s", kind, msgid)
        db.log_event("learn_failed", msgid, detail=kind)
        return False
    now = int(time.time())
    with db.tx():
        db.update_message(msgid, learned_as=kind, learned_at=now, pending_learn=None, pending_learn_at=None)
        db.record_rate("learn")
        db.log_event(f"learn_{kind}", msgid, detail=reason)
    log.info("learned %s as %s (%s)", msgid, kind, reason)
    return True


# ----- scan / poll / drain -------------------------------------------------


def scan_inbox(client: IMAPClient, db: Db, log: logging.Logger, acc: Account, fmap: dict[str, str]) -> None:
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

    if not candidates:
        return

    fetched = client.fetch(candidates, [b"BODY.PEEK[]", b"FLAGS"])
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
            db.upsert_message(msgid, fmap["inbox"], sender, subject)

        # Detect Junk -> Inbox revert (user moved a message we previously
        # placed in Junk, or a message that lived in Junk, back to Inbox).
        reverted = prior_folder == fmap["junk"]
        if reverted and uid in unseen:
            # User intent confirmed by $NotJunk keyword skips grace.
            if NOTJUNK_KEYWORD in flags:
                try_learn(db, log, acc, raw, msgid, "ham", reason="revert+notjunk_kw")
            else:
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
        score = rspamd_scan(raw, recipient, acc.reject_score_above)
        if score is None:
            log.warning("scan failed for %s (uid=%s) - keeping in inbox", msgid, uid)
            db.log_event("scan_failed", msgid)
            continue

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
                client.add_flags(uid, [b"\\Flagged"])
                with db.tx():
                    db.add_pending_move(uv, uid, msgid)
                    db.update_message(msgid, our_action="flagged")
                    db.log_event("flag_pending_move", msgid, detail=f"score={score:.2f}")


def execute_due_moves(client: IMAPClient, db: Db, log: logging.Logger, acc: Account, fmap: dict[str, str]) -> None:
    if acc.mode != "move":
        return
    if db.in_safe_mode("all"):
        return
    uv = select_with_uidvalidity_check(client, db, fmap["inbox"], log)
    rows = db.due_pending_moves(uv, acc.move_grace_seconds)
    if not rows:
        return
    if not check_rate_or_safe_mode(db, log, "move", acc.max_moves_per_hour):
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

    # Cap one batch to avoid blasting if many accumulated.
    to_move = to_move[: acc.max_moves_per_hour]
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
    fetched = client.fetch(uids, [b"BODY.PEEK[]", b"FLAGS"])
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
            db.upsert_message(msgid, fmap["junk"], sender, subject)

        # Filter put it here: nothing to learn. Skip.
        if prior is not None and prior["our_action"] == "moved_to_junk":
            continue
        # New-to-us-or-was-elsewhere: user must have moved this Inbox -> Junk.
        if prior_folder == fmap["inbox"] or prior is None:
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
            try_learn(db, log, acc, raw, msgid, kind, reason="grace_elapsed")


def drain_train_spam(client: IMAPClient, db: Db, log: logging.Logger, acc: Account, fmap: dict[str, str]) -> None:
    if not acc.learn_from_moves:
        return
    folder = fmap["spam_train"]
    try:
        select_with_uidvalidity_check(client, db, folder, log)
    except IMAPClientError as ex:
        log.warning("select %s failed: %s", folder, ex)
        return
    uids = client.search(["ALL"])
    if not uids:
        return
    uids = uids[: acc.max_train_per_run]
    fetched = client.fetch(uids, [b"BODY.PEEK[]"])
    learned_uids: list[int] = []
    for uid, data in fetched.items():
        if SHUTDOWN.is_set():
            return
        raw = data.get(b"BODY[]") or data.get(b"BODY.PEEK[]")
        if not raw:
            continue
        msgid, subject, sender = parse_envelope(raw)
        msgid = msgid or f"<no-msgid-uid-{uid}>"
        with db.tx():
            db.upsert_message(msgid, folder, sender, subject)
        ok = try_learn(db, log, acc, raw, msgid, "spam", reason="train_spam_folder")
        if ok:
            learned_uids.append(uid)
    if not learned_uids:
        return
    try:
        client.move(learned_uids, fmap["trained_spam"])
    except IMAPClientError as ex:
        log.warning("move %s -> %s failed: %s", folder, fmap["trained_spam"], ex)
        return
    now = int(time.time())
    with db.tx():
        for uid in learned_uids:
            db.log_event("trained_moved", detail=f"uid={uid} -> {fmap['trained_spam']}")
        db.conn.execute(
            "UPDATE messages SET current_folder=? WHERE account=? AND current_folder=? AND learned_as='spam' AND learned_at>=?",
            (fmap["trained_spam"], db.account, folder, now - 60),
        )
    log.info("drain_train_spam: learned+moved %d", len(learned_uids))


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
    cutoff_date = time.strftime("%d-%b-%Y", time.gmtime(time.time() - days * 86400))
    try:
        uids = client.search(["BEFORE", cutoff_date])
    except IMAPClientError as ex:
        log.warning("retention: search BEFORE %s failed: %s", cutoff_date, ex)
        return
    if not uids:
        return
    uids = uids[:500]
    to_move: list[int] = []
    if exclude_learned_ham:
        fetched = client.fetch(uids, [b"BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)]"])
        for uid, data in fetched.items():
            raw = (
                data.get(b"BODY[HEADER.FIELDS (MESSAGE-ID)]")
                or data.get(b"BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)]")
                or b""
            )
            msgid, _, _ = parse_envelope(raw)
            if msgid:
                row = db.get_message(msgid)
                if row is not None and row["learned_as"] == "ham":
                    continue
            to_move.append(uid)
    else:
        to_move = list(uids)
    if not to_move:
        return
    try:
        client.move(to_move, fmap["trash"])
    except IMAPClientError as ex:
        log.warning("retention: move %s -> %s failed: %s", src, fmap["trash"], ex)
        return
    with db.tx():
        db.log_event(tag, detail=f"moved {len(to_move)} from {src} to {fmap['trash']}")
    log.info("retention sweep %s: moved %d %s -> %s", tag, len(to_move), src, fmap["trash"])


# ----- main loop ----------------------------------------------------------


def account_loop(acc: Account) -> None:
    log = logging.getLogger(acc.name)
    threading.current_thread().name = acc.name
    db = Db(acc.name)
    state = AccountState()
    backoff = RECONNECT_MIN_BACKOFF

    while not SHUTDOWN.is_set():
        client: IMAPClient | None = None
        try:
            log.info("connecting to %s:%d as %s", acc.imap_host, acc.imap_port, acc.user)
            client = IMAPClient(acc.imap_host, port=acc.imap_port, ssl=acc.ssl, timeout=60)
            client.login(acc.user, acc.password)
            delim = detect_delimiter(client)
            acc.delimiter = delim
            if acc.auto_special_folders:
                detected = detect_special_folders(client)
                for key in ("junk", "trash"):
                    name = detected.get(key)
                    if not name:
                        continue
                    current = getattr(acc, key)
                    if name != current:
                        log.info("auto-detected %s folder via SPECIAL-USE: %s (was %s)",
                                 key, name, current)
                        setattr(acc, key, name)
                # Keep spam_train/trained_spam under whatever the new junk is,
                # if the configured names referenced "Junk/..." literally.
                if "junk" in detected:
                    new_junk = detected["junk"]
                    for key in ("spam_train", "trained_spam"):
                        cur = getattr(acc, key)
                        if cur.startswith("Junk/"):
                            setattr(acc, key, new_junk + cur[len("Junk"):])
            acc.folder_map = build_folder_map(acc, delim)
            ensure_folders(client, log, acc.folder_map)
            log.info("connected, delimiter=%r, mode=%s", delim, acc.mode)
            backoff = RECONNECT_MIN_BACKOFF

            while not SHUTDOWN.is_set():
                heartbeat()
                db.prune_rate()

                drain_train_spam(client, db, log, acc, acc.folder_map)
                if SHUTDOWN.is_set():
                    break

                scan_inbox(client, db, log, acc, acc.folder_map)
                if SHUTDOWN.is_set():
                    break
                execute_due_moves(client, db, log, acc, acc.folder_map)

                now = time.monotonic()
                if now - state.last_junk_poll >= acc.junk_poll_interval:
                    poll_junk(client, db, log, acc, acc.folder_map)
                    state.last_junk_poll = now

                if now - state.last_retention >= acc.retention_check_interval:
                    retention_sweep(client, db, log, acc, acc.folder_map)
                    state.last_retention = now

                # IDLE on Inbox until activity, junk_poll_interval, or idle_timeout.
                client.select_folder(acc.folder_map["inbox"])
                wait = min(acc.idle_timeout, max(30, acc.junk_poll_interval))
                try:
                    client.idle()
                    client.idle_check(timeout=wait)
                finally:
                    try:
                        client.idle_done()
                    except IMAPClientError:
                        pass
        except (IMAPClientError, OSError) as ex:
            log.warning("connection error: %s (backoff %ds)", ex, backoff)
            db.log_event("conn_error", detail=str(ex)[:300])
        except Exception as ex:  # noqa: BLE001 - last-resort guard
            log.exception("unhandled error in account loop: %s", ex)
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

    db.close()
    log.info("thread exiting")


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
    accounts = load_accounts(CONFIG_PATH)
    log.info("loaded %d account(s): %s", len(accounts), ", ".join(a.name for a in accounts))
    heartbeat()

    threads: list[threading.Thread] = []
    for acc in accounts:
        t = threading.Thread(target=account_loop, args=(acc,), name=acc.name, daemon=False)
        t.start()
        threads.append(t)

    # Heartbeat watchdog: keep main thread alive, update heartbeat periodically
    # so it advances even if every account thread is stuck in IDLE.
    while not SHUTDOWN.is_set():
        heartbeat()
        SHUTDOWN.wait(timeout=60)

    for t in threads:
        t.join(timeout=30)
    log.info("all threads exited")
    return 0


if __name__ == "__main__":
    sys.exit(main())
