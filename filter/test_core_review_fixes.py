"""Regression coverage for the safe core CURSOR_CODE_REVIEW fixes."""

import logging
import os
import sqlite3
import tempfile
import time

os.environ.setdefault("STATE_DIR", tempfile.mkdtemp(prefix="sf_test_"))

import pytest  # noqa: E402
from imapclient.exceptions import IMAPClientError  # noqa: E402

import filter as f  # noqa: E402
from test_connection import _write_accounts  # noqa: E402
from test_fetch_discipline import CapIMAP, _body_fetch_uids  # noqa: E402
from test_inbox_bookmark import _raw  # noqa: E402
from test_learn import _FakeIMAP, _mk_account, _pending  # noqa: E402
from test_shadow_mode import FMAP, _all_existing, _mk_db  # noqa: E402


LOG = logging.getLogger("test")


def _legacy_messages_schema(*, object_pk: bool) -> str:
    identity = (
        """
        folder TEXT NOT NULL,
        uidvalidity INTEGER NOT NULL,
        uid INTEGER NOT NULL,
        message_id TEXT,
        """
        if object_pk
        else "message_id TEXT NOT NULL,"
    )
    primary_key = (
        "PRIMARY KEY (account, folder, uidvalidity, uid)"
        if object_pk
        else "PRIMARY KEY (account, message_id)"
    )
    return f"""
        CREATE TABLE messages (
            account TEXT NOT NULL,
            {identity}
            first_seen INTEGER NOT NULL,
            last_seen INTEGER NOT NULL,
            current_folder TEXT NOT NULL,
            moved_to_junk_at INTEGER,
            our_score REAL,
            our_action TEXT,
            learned_as TEXT,
            learned_at INTEGER,
            pending_learn TEXT,
            pending_learn_at INTEGER,
            sender TEXT,
            subject TEXT,
            {primary_key}
        );
    """


@pytest.mark.parametrize("object_pk", [False, True])
def test_init_db_migrates_supported_legacy_schemas(tmp_path, monkeypatch, object_pk):
    db_path = tmp_path / "spamfilter.db"
    monkeypatch.setattr(f, "STATE_DIR", tmp_path)
    monkeypatch.setattr(f, "DB_PATH", db_path)
    with sqlite3.connect(db_path) as conn:
        conn.executescript(_legacy_messages_schema(object_pk=object_pk))
        conn.executescript(
            """
            CREATE TABLE pending_move (
                account TEXT NOT NULL,
                uidvalidity INTEGER NOT NULL,
                uid INTEGER NOT NULL,
                message_id TEXT NOT NULL,
                flag_at INTEGER NOT NULL,
                PRIMARY KEY (account, uidvalidity, uid)
            );
            """
        )
        if object_pk:
            conn.execute(
                """
                INSERT INTO messages(
                    account, folder, uidvalidity, uid, message_id,
                    first_seen, last_seen, current_folder
                ) VALUES(?,?,?,?,?,?,?,?)
                """,
                ("acct", "INBOX", 7, 9, "legacy@example", 1, 2, "INBOX"),
            )
            conn.execute(
                "INSERT INTO pending_move VALUES(?,?,?,?,?)",
                ("acct", 7, 9, "legacy@example", 1),
            )
        else:
            conn.execute(
                """
                INSERT INTO messages(
                    account, message_id, first_seen, last_seen, current_folder
                ) VALUES(?,?,?,?,?)
                """,
                ("acct", "legacy@example", 1, 2, "INBOX"),
            )

    f.init_db()

    with sqlite3.connect(db_path) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
        assert {
            "folder", "uidvalidity", "uid", "body_sha256", "received_at",
            "learn_retry_count", "learn_retry_at",
        } <= cols
        assert conn.execute(
            "SELECT message_id FROM messages"
        ).fetchone()[0] == "legacy@example"
        indexes = {
            row[1] for row in conn.execute("PRAGMA index_list(messages)")
        }
        assert {"idx_messages_msgid", "idx_messages_sha"} <= indexes
        pending_info = list(conn.execute("PRAGMA table_info(pending_move)"))
        pending_cols = {row[1] for row in pending_info}
        assert "folder" in pending_cols
        assert next(
            row[3] for row in pending_info if row[1] == "folder"
        ) == 1
        assert [
            row[1] for row in sorted(
                (row for row in pending_info if row[5]),
                key=lambda row: row[5],
            )
        ] == ["account", "folder", "uidvalidity", "uid"]
        if object_pk:
            assert conn.execute(
                "SELECT folder FROM pending_move"
            ).fetchone()[0] == "INBOX"
        conn.execute(
            """
            INSERT INTO pending_move(
                account, folder, uidvalidity, uid, message_id, flag_at
            ) VALUES(?,?,?,?,?,?)
            """,
            ("acct", "Junk", 8, 10, "junk@example", 1),
        )
        conn.execute(
            """
            INSERT INTO pending_move(
                account, folder, uidvalidity, uid, message_id, flag_at
            ) VALUES(?,?,?,?,?,?)
            """,
            ("acct", "Trash", 8, 10, "trash@example", 1),
        )
        assert conn.execute(
            "SELECT COUNT(*) FROM pending_move WHERE uidvalidity=8 AND uid=10"
        ).fetchone()[0] == 2
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO pending_move(
                    account, folder, uidvalidity, uid, message_id, flag_at
                ) VALUES(?,?,?,?,?,?)
                """,
                ("acct", None, 8, 11, "null@example", 1),
            )


class _RFCStarIMAP(CapIMAP):
    def search(self, criteria):
        self.searches.append(list(criteria))
        if criteria and criteria[0] == "UID":
            return [10]  # RFC '*' resolves to the old highest UID.
        return list(self.uids)


def test_inbox_rejects_rfc_star_uid_at_bookmark(tmp_path):
    db = _mk_db(tmp_path)
    with db.tx():
        db.set_scan_bookmark("INBOX", 1, 10)
    client = _RFCStarIMAP(
        existing=_all_existing(), uids=[10], bodies={10: _raw(10)}
    )

    f.scan_inbox(client, db, LOG, _mk_account(), FMAP)

    assert db.get_scan_bookmark("INBOX", 1) == 10
    assert client.fetch_calls == []


class _ArrivalBetweenSearchesIMAP(CapIMAP):
    def search(self, criteria):
        self.searches.append(list(criteria))
        if criteria and criteria[0] == "UNSEEN":
            return [1]  # Old implementation's first snapshot.
        if criteria and criteria[0] == "UID":
            return [1, 2]  # UID 2 arrived before the second old search.
        return []


def test_inbox_uses_fetched_flags_not_two_search_race(tmp_path, monkeypatch):
    db = _mk_db(tmp_path)
    with db.tx():
        db.set_scan_bookmark("INBOX", 1, 0)
    monkeypatch.setattr(f, "rspamd_scan", lambda *a, **k: 1.0)
    client = _ArrivalBetweenSearchesIMAP(
        existing=_all_existing(),
        uids=[1, 2],
        bodies={1: _raw(1), 2: _raw(2)},
    )

    f.scan_inbox(client, db, LOG, _mk_account(), FMAP)

    assert not any(search[0] == "UNSEEN" for search in client.searches)
    assert db.get_imap_message("INBOX", 1, 1)["our_score"] == 1.0
    assert db.get_imap_message("INBOX", 1, 2)["our_score"] == 1.0
    assert db.get_scan_bookmark("INBOX", 1) == 2


class _FailFlagOnceIMAP(CapIMAP):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.fail_flag = True

    def add_flags(self, uid, flags):
        if self.fail_flag:
            self.fail_flag = False
            raise IMAPClientError("STORE failed")
        super().add_flags(uid, flags)


def test_persisted_score_replays_failed_flag_action(tmp_path, monkeypatch):
    db = _mk_db(tmp_path)
    with db.tx():
        db.set_scan_bookmark("INBOX", 1, 0)
    scans = {"count": 0}

    def scan(*args, **kwargs):
        scans["count"] += 1
        return 9.0

    monkeypatch.setattr(f, "rspamd_scan", scan)
    client = _FailFlagOnceIMAP(
        existing=_all_existing(), uids=[1], bodies={1: _raw(1)}
    )

    with pytest.raises(IMAPClientError, match="STORE failed"):
        f.scan_inbox(client, db, LOG, _mk_account(mode="flag"), FMAP)
    row = db.get_imap_message("INBOX", 1, 1)
    assert row["our_score"] == 9.0
    assert row["our_action"] is None
    assert db.get_scan_bookmark("INBOX", 1) == 0

    f.scan_inbox(client, db, LOG, _mk_account(mode="flag"), FMAP)
    assert scans["count"] == 1
    assert client.flags_added == [(1, [b"\\Flagged"])]
    assert db.get_imap_message("INBOX", 1, 1)["our_action"] == "flagged"
    assert db.get_scan_bookmark("INBOX", 1) == 1


def test_persisted_score_replays_failed_move_intent(tmp_path, monkeypatch):
    db = _mk_db(tmp_path)
    with db.tx():
        db.set_scan_bookmark("INBOX", 1, 0)
    monkeypatch.setattr(f, "rspamd_scan", lambda *a, **k: 9.0)
    client = CapIMAP(
        existing=_all_existing(), uids=[1], bodies={1: _raw(1)}
    )
    original = db.add_pending_move
    monkeypatch.setattr(
        db, "add_pending_move",
        lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("disk")),
    )

    with pytest.raises(sqlite3.OperationalError, match="disk"):
        f.scan_inbox(client, db, LOG, _mk_account(mode="move"), FMAP)
    assert db.get_imap_message("INBOX", 1, 1)["our_score"] == 9.0
    assert db.get_imap_message("INBOX", 1, 1)["our_action"] is None

    monkeypatch.setattr(db, "add_pending_move", original)
    f.scan_inbox(client, db, LOG, _mk_account(mode="move"), FMAP)
    assert db.get_imap_message("INBOX", 1, 1)["our_action"] == "pending_move"
    assert db.conn.execute("SELECT COUNT(*) FROM pending_move").fetchone()[0] == 1


class _FlagsIMAP(CapIMAP):
    def __init__(self, *, flags=None, **kwargs):
        super().__init__(**kwargs)
        self.uid_flags = dict(flags or {})

    def fetch(self, uids, parts):
        out = super().fetch(uids, parts)
        parts_b = [p if isinstance(p, bytes) else str(p).encode() for p in parts]
        if b"FLAGS" in parts_b:
            for uid in uids:
                out.setdefault(uid, {})[b"FLAGS"] = self.uid_flags.get(uid, ())
        return out


def _seed_inbox_sibling(db, raw, *, action=None):
    with db.tx():
        db.upsert_imap_message(
            "INBOX", 1, 1,
            message_id="mid4@example.com", body_sha256=f.body_sha256(raw),
        )
        if action is not None:
            db.update_imap_message("INBOX", 1, 1, our_action=action)


def test_pending_move_provenance_is_not_learned_from_junk(tmp_path, monkeypatch):
    raw = _raw(4)
    db = _mk_db(tmp_path)
    _seed_inbox_sibling(db, raw, action="pending_move")
    with db.tx():
        db.set_scan_bookmark("Junk", 1, 3)
    calls = {"count": 0}
    monkeypatch.setattr(
        f, "rspamd_learn",
        lambda *a, **k: calls.__setitem__("count", calls["count"] + 1),
    )
    client = _FlagsIMAP(
        existing=_all_existing(), uids=[4], bodies={4: raw},
        flags={4: (b"$Junk",)},
    )

    f.poll_junk(client, db, LOG, _mk_account(), FMAP)

    row = db.get_imap_message("Junk", 1, 4)
    assert row["pending_learn"] is None
    assert calls["count"] == 0
    assert db.get_scan_bookmark("Junk", 1) == 4


def test_failed_immediate_keyword_learn_is_queued(tmp_path, monkeypatch):
    raw = _raw(4)
    db = _mk_db(tmp_path)
    _seed_inbox_sibling(db, raw)
    with db.tx():
        db.set_scan_bookmark("Junk", 1, 3)
    monkeypatch.setattr(f, "rspamd_learn", lambda *a, **k: "error")
    client = _FlagsIMAP(
        existing=_all_existing(), uids=[4], bodies={4: raw},
        flags={4: (b"$Junk",)},
    )

    f.poll_junk(client, db, LOG, _mk_account(learn_grace_seconds=300), FMAP)

    row = db.get_imap_message("Junk", 1, 4)
    assert row["pending_learn"] == "spam"
    assert row["learn_retry_count"] == 1
    assert row["learn_retry_at"] > int(time.time())
    assert row["learned_as"] is None


def test_junk_missing_body_retries_terminal_prefix(tmp_path):
    db = _mk_db(tmp_path)
    with db.tx():
        db.set_scan_bookmark("Junk", 1, 3)
    client = CapIMAP(
        existing=_all_existing(), uids=[4, 5],
        bodies={4: _raw(4)}, sizes={4: len(_raw(4)), 5: 100},
    )

    f.poll_junk(client, db, LOG, _mk_account(), FMAP)
    assert db.get_scan_bookmark("Junk", 1) == 4

    client.bodies[5] = _raw(5)
    client.sizes[5] = len(_raw(5))
    f.poll_junk(client, db, LOG, _mk_account(), FMAP)
    assert db.get_scan_bookmark("Junk", 1) == 5


class _SelectUV:
    def __init__(self, uidvalidity):
        self.uidvalidity = uidvalidity

    def select_folder(self, folder, readonly=False):
        return {b"UIDVALIDITY": self.uidvalidity}


def test_pending_move_cleanup_is_folder_scoped(tmp_path):
    db = _mk_db(tmp_path)
    with db.tx():
        db.set_uidvalidity("Junk", 1)
        db.upsert_imap_message("INBOX", 1, 9, message_id="move@example")
        db.update_imap_message("INBOX", 1, 9, our_action="pending_move")
        db.upsert_imap_message("Junk", 1, 9, message_id="collision@example")
        db.add_pending_move(1, 9, "move@example")

    f.select_with_uidvalidity_check(_SelectUV(2), db, "Junk", LOG, readonly=True)

    assert db.conn.execute("SELECT COUNT(*) FROM pending_move").fetchone()[0] == 1


def test_pending_learn_rejects_stale_uidvalidity(tmp_path, monkeypatch):
    db = _mk_db(tmp_path)
    _pending(db, "stale@example", "INBOX", "ham", int(time.time()) - 10, uv=1)
    with db.tx():
        db.set_uidvalidity("INBOX", 1)
    calls = {"count": 0}
    monkeypatch.setattr(
        f, "rspamd_learn",
        lambda *a, **k: calls.__setitem__("count", calls["count"] + 1),
    )
    client = _FakeIMAP({1: b"wrong epoch"}, uidvalidity=2)

    f.process_pending_learns(
        client, db, LOG, _mk_account(learn_grace_seconds=0),
        {"junk": "Junk", "inbox": "INBOX"},
    )

    assert calls["count"] == 0
    assert client.fetches == []
    assert db.get_imap_message("INBOX", 1, 1)["pending_learn"] is None


@pytest.mark.parametrize("message_id", ["duplicate@example", None])
def test_retry_state_isolated_by_imap_object(tmp_path, message_id):
    db = _mk_db(tmp_path)
    with db.tx():
        for uid in (1, 2):
            db.upsert_imap_message(
                "INBOX", 7, uid, message_id=message_id
            )
        f._set_learn_retry(db, "INBOX", 7, 1)

    first = db.get_imap_message("INBOX", 7, 1)
    second = db.get_imap_message("INBOX", 7, 2)
    assert first["learn_retry_count"] == 1
    assert second["learn_retry_count"] == 0
    assert second["learn_retry_at"] is None


def test_object_retry_backoff_is_bounded(tmp_path, monkeypatch):
    db = _mk_db(tmp_path)
    now = 1_700_000_000
    monkeypatch.setattr(f.time, "time", lambda: now)
    with db.tx():
        db.upsert_imap_message("INBOX", 7, 1, message_id=None)
        for _ in range(20):
            f._set_learn_retry(db, "INBOX", 7, 1)

    row = db.get_imap_message("INBOX", 7, 1)
    assert row["learn_retry_count"] == 16
    assert row["learn_retry_at"] == now + f.LEARN_RETRY_MAX_S


def test_train_infrastructure_failure_stays_put(tmp_path, monkeypatch):
    db = _mk_db(tmp_path)
    calls = {"count": 0}

    def fail(*args, **kwargs):
        calls["count"] += 1
        return "error"

    monkeypatch.setattr(f, "rspamd_learn", fail)
    client = CapIMAP(
        existing=_all_existing(), uids=[7], bodies={7: _raw(7)}
    )
    acc = _mk_account()

    f._drain_train_folder(
        client, db, LOG, acc, FMAP,
        kind="spam", src_key="spam_train", dst_key="trained_spam",
    )
    f._drain_train_folder(
        client, db, LOG, acc, FMAP,
        kind="spam", src_key="spam_train", dst_key="trained_spam",
    )

    row = db.get_imap_message(FMAP["spam_train"], 1, 7)
    assert calls["count"] == 1
    assert client.moved == []
    assert row["learned_as"] is None
    assert row["learn_retry_count"] == 1


def test_train_cap_applies_after_retry_backoff_filter(tmp_path, monkeypatch):
    db = _mk_db(tmp_path)
    now = 1_700_000_000
    monkeypatch.setattr(f.time, "time", lambda: now)
    with db.tx():
        db.upsert_imap_message(
            FMAP["spam_train"], 1, 7, message_id="backed-off@example"
        )
        f._set_learn_retry(db, FMAP["spam_train"], 1, 7)

    monkeypatch.setattr(f, "rspamd_learn", lambda *args, **kwargs: "learned")
    client = CapIMAP(
        existing=_all_existing(),
        uids=[7, 8],
        bodies={7: _raw(7), 8: _raw(8)},
    )
    acc = _mk_account()
    acc.max_train_per_run = 1

    f._drain_train_folder(
        client, db, LOG, acc, FMAP,
        kind="spam", src_key="spam_train", dst_key="trained_spam",
    )

    assert client.moved == [([8], FMAP["trained_spam"])]


def test_auth_failure_backs_off_without_terminal_state(tmp_path, monkeypatch):
    db = _mk_db(tmp_path)
    _pending(db, "auth@example", "Junk", "spam", int(time.time()) - 10)
    monkeypatch.setattr(f, "rspamd_learn", lambda *a, **k: "auth")

    assert not f.try_learn(
        db, LOG, _mk_account(), b"raw", "auth@example", "spam", "test",
        folder="Junk", uidvalidity=1, uid=1,
    )

    row = db.get_imap_message("Junk", 1, 1)
    assert not db.in_safe_mode("learning")
    assert row["pending_learn"] == "spam"
    assert row["learned_as"] is None
    assert row["learn_retry_count"] == 1


class _ScanResp:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"score": True},
        {"score": float("nan")},
        {"score": float("inf")},
        {"score": 10 ** 1000},
        {"score": "9.0"},
    ],
)
def test_rspamd_scan_rejects_malformed_or_nonfinite_score(monkeypatch, payload):
    monkeypatch.setattr(
        f.requests, "post", lambda *a, **k: _ScanResp(payload)
    )
    assert f.rspamd_scan(_raw(1), "u@example.com", 100.0) is None


@pytest.mark.parametrize("value", [".nan", ".inf", "true"])
def test_config_rejects_nonfinite_or_boolean_threshold(tmp_path, value):
    path = _write_accounts(tmp_path, f"    threshold: {value}\n")
    with pytest.raises(SystemExit, match="threshold"):
        f.load_accounts(path)


def test_config_rejects_boolean_integer(tmp_path):
    path = _write_accounts(tmp_path, "    idle_timeout: true\n")
    with pytest.raises(SystemExit, match="idle_timeout"):
        f.load_accounts(path)


def test_account_ssl_overrides_default_tls_mode(tmp_path):
    path = _write_accounts(
        tmp_path, "    ssl: false\n", defaults="  tls_mode: implicit\n"
    )
    assert f.load_accounts(path)[0].tls_mode == "starttls"


@pytest.mark.parametrize(
    ("location", "text", "match"),
    [
        ("root", "junk_retention_day: 1\n", "unknown key"),
        ("defaults", "  junk_retention_day: 1\n", "junk_retention_days"),
        ("account", "    junk_retention_day: 1\n", "junk_retention_days"),
    ],
)
def test_unknown_config_keys_are_rejected(tmp_path, location, text, match):
    if location == "root":
        path = _write_accounts(tmp_path, "")
        path.write_text(path.read_text() + text)
    elif location == "defaults":
        path = _write_accounts(tmp_path, "", defaults=text)
    else:
        path = _write_accounts(tmp_path, text)
    with pytest.raises(SystemExit, match=match):
        f.load_accounts(path)


@pytest.mark.parametrize(
    "line",
    [
        "    idle_timeout: 0\n",
        "    idle_timeout: 1741\n",
        "    poll_interval: 0\n",
        "    junk_poll_interval: -1\n",
        "    retention_check_interval: 604801\n",
    ],
)
def test_loop_timing_bounds_are_enforced(tmp_path, line):
    path = _write_accounts(tmp_path, line)
    with pytest.raises(SystemExit, match="out of range"):
        f.load_accounts(path)
