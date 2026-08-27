"""IMAP-object identity: (folder, uidvalidity, uid), not Message-ID.

Two UIDs that share a Message-ID score independently and must not
inherit each other's Junk/Inbox history. Pending learns FETCH the
stored UID; HEADER Message-ID search is not used.

Run: STATE_DIR=/tmp/x python -m pytest test_message_identity.py
"""

import logging
import os
import sqlite3
import tempfile
import time

os.environ.setdefault("STATE_DIR", tempfile.mkdtemp(prefix="sf_test_"))

import filter as f  # noqa: E402

from test_fetch_discipline import CapIMAP, _events  # noqa: E402
from test_learn import _FakeIMAP, _pending  # noqa: E402
from test_shadow_mode import FMAP, _all_existing, _mk_account, _mk_db  # noqa: E402

LOG = logging.getLogger("test")

DUP_MID = "dup@example.com"


def _raw(body: bytes) -> bytes:
    return (
        b"From: sender@example.com\r\n"
        b"To: u@example.com\r\n"
        b"Subject: collision\r\n"
        b"Message-ID: <" + DUP_MID.encode() + b">\r\n"
        b"\r\n"
        + body
    )


def test_duplicate_message_id_scores_independently(tmp_path, monkeypatch):
    db = _mk_db(tmp_path)
    acc = _mk_account(mode="shadow")
    with db.tx():
        db.set_scan_bookmark("INBOX", 1, 0)
    scores: list[bytes] = []

    def scan(raw, *a, **k):
        scores.append(raw)
        return 4.0 if b"body-a" in raw else 7.0

    monkeypatch.setattr(f, "rspamd_scan", scan)
    client = CapIMAP(
        existing=_all_existing(),
        uids=[1, 2],
        bodies={1: _raw(b"body-a\r\n"), 2: _raw(b"body-b\r\n")},
    )

    f.scan_inbox(client, db, LOG, acc, FMAP)

    assert len(scores) == 2
    row1 = db.get_imap_message("INBOX", 1, 1)
    row2 = db.get_imap_message("INBOX", 1, 2)
    assert row1["message_id"] == DUP_MID
    assert row2["message_id"] == DUP_MID
    assert row1["our_score"] == 4.0
    assert row2["our_score"] == 7.0
    assert row1["body_sha256"] != row2["body_sha256"]
    assert len(db.find_by_message_id(DUP_MID)) == 2


def test_duplicate_message_id_does_not_inherit_junk_history(tmp_path, monkeypatch):
    db = _mk_db(tmp_path)
    acc = _mk_account(mode="shadow")
    raw_a = _raw(b"body-a\r\n")
    raw_b = _raw(b"body-b\r\n")
    with db.tx():
        db.set_scan_bookmark("INBOX", 1, 0)
        db.upsert_imap_message(
            "Junk", 1, 9,
            message_id=DUP_MID, sender="s@example.com", subject="collision",
            body_sha256=f.body_sha256(raw_a),
        )
        db.update_imap_message(
            "Junk", 1, 9,
            current_folder="Junk",
            our_action="moved_to_junk",
            learned_as="spam",
            our_score=9.0,
        )
    monkeypatch.setattr(f, "rspamd_scan", lambda *a, **k: 1.0)
    client = CapIMAP(
        existing=_all_existing(),
        uids=[1],
        bodies={1: raw_b},
    )

    f.scan_inbox(client, db, LOG, acc, FMAP)

    inbox = db.get_imap_message("INBOX", 1, 1)
    assert inbox is not None
    assert inbox["our_score"] == 1.0
    assert inbox["pending_learn"] is None
    assert inbox["learned_as"] is None
    assert "pending_ham" not in _events(db)
    junk = db.get_imap_message("Junk", 1, 9)
    assert junk["learned_as"] == "spam"
    assert junk["our_score"] == 9.0


def test_pending_learn_fetches_stored_uid_not_header_search(tmp_path, monkeypatch):
    db = _mk_db(tmp_path)
    acc = _mk_account(learn_grace_seconds=0)
    _pending(db, "loop1", "INBOX", "ham", int(time.time()) - 10, uid=1)
    monkeypatch.setattr(f, "rspamd_learn", lambda *a, **k: "learned")
    # HEADER search would return a UID that has no body; FETCH of the
    # stored UID 1 must still succeed.
    client = _FakeIMAP({1: b"raw-bytes"}, header_hits=[99])
    fmap = {"junk": "Junk", "inbox": "INBOX"}

    f.process_pending_learns(client, db, LOG, acc, fmap)

    row = db.get_imap_message("INBOX", 1, 1)
    assert row["learned_as"] == "ham"
    assert row["pending_learn"] is None
    assert not any(s and s[0] == "HEADER" for s in client.searches)


def test_migrate_old_pk_copies_rows(tmp_path):
    db_path = tmp_path / "spamfilter.db"
    f.DB_PATH = db_path
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE messages (
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
                pending_learn      TEXT,
                pending_learn_at   INTEGER,
                sender             TEXT,
                subject            TEXT,
                received_at        INTEGER,
                PRIMARY KEY (account, message_id)
            );
            """
        )
        conn.execute(
            """
            INSERT INTO messages(
                account, message_id, first_seen, last_seen, current_folder,
                sender, subject, received_at
            ) VALUES(?,?,?,?,?,?,?,?)
            """,
            ("acct", "old@id", 1, 2, "INBOX", "s", "subj", 3),
        )
        f._migrate(conn)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(messages)")}
        assert "uid" in cols
        assert "folder" in cols
        assert "body_sha256" in cols
        row = conn.execute(
            "SELECT account, folder, uidvalidity, uid, message_id, current_folder "
            "FROM messages"
        ).fetchone()
        assert row[0] == "acct"
        assert row[1] == "INBOX"
        assert row[2] == 0
        assert row[3] >= 1
        assert row[4] == "old@id"
        assert row[5] == "INBOX"
        pk = conn.execute("PRAGMA table_info(messages)").fetchall()
        pk_cols = [r[1] for r in sorted(pk, key=lambda r: r[5] or 99) if r[5]]
        assert pk_cols == ["account", "folder", "uidvalidity", "uid"]
