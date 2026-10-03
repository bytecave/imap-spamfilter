"""Inbox scan_bookmark advances only through a terminal UID prefix.

Run: STATE_DIR=/tmp/x python -m pytest test_inbox_bookmark.py
"""

import logging
import os
import tempfile

os.environ.setdefault("STATE_DIR", tempfile.mkdtemp(prefix="sf_test_"))

import filter as f  # noqa: E402

from test_fetch_discipline import CapIMAP, _events  # noqa: E402
from test_shadow_mode import FMAP, _all_existing, _mk_account, _mk_db  # noqa: E402

LOG = logging.getLogger("test")


def _raw(n: int) -> bytes:
    return (
        f"From: sender@example.com\r\n"
        f"To: u@example.com\r\n"
        f"Subject: msg {n}\r\n"
        f"Message-ID: <mid{n}@example.com>\r\n"
        f"\r\n"
        f"body {n}\r\n"
    ).encode()


def test_bookmark_stops_at_rspamd_failure(tmp_path, monkeypatch):
    db = _mk_db(tmp_path)
    acc = _mk_account(mode="shadow")
    with db.tx():
        db.set_scan_bookmark("INBOX", 1, 0)

    def scan(raw, *a, **k):
        if b"mid2@example.com" in raw:
            return None
        return f.ScanResult(1.0, (), None)

    monkeypatch.setattr(f, "rspamd_scan_detail", scan)
    client = CapIMAP(
        existing=_all_existing(),
        uids=[1, 2, 3],
        bodies={1: _raw(1), 2: _raw(2), 3: _raw(3)},
    )

    f.scan_inbox(client, db, LOG, acc, FMAP)

    assert db.get_scan_bookmark("INBOX", 1) == 1
    assert db.get_imap_message("INBOX", 1, 1)["our_score"] == 1.0
    row2 = db.get_imap_message("INBOX", 1, 2)
    assert row2 is None or row2["our_score"] is None
    assert db.get_imap_message("INBOX", 1, 3) is None
    assert "scan_failed" in _events(db)


def test_bookmark_advances_after_rspamd_recovers(tmp_path, monkeypatch):
    db = _mk_db(tmp_path)
    acc = _mk_account(mode="shadow")
    with db.tx():
        db.set_scan_bookmark("INBOX", 1, 0)

    def down(raw, *a, **k):
        if b"mid2@example.com" in raw:
            return None
        return f.ScanResult(1.0, (), None)

    monkeypatch.setattr(f, "rspamd_scan_detail", down)
    client = CapIMAP(
        existing=_all_existing(),
        uids=[1, 2, 3],
        bodies={1: _raw(1), 2: _raw(2), 3: _raw(3)},
    )
    f.scan_inbox(client, db, LOG, acc, FMAP)
    assert db.get_scan_bookmark("INBOX", 1) == 1

    monkeypatch.setattr(f, "rspamd_scan_detail", lambda *a, **k: f.ScanResult(1.0, (), None))
    f.scan_inbox(client, db, LOG, acc, FMAP)

    assert db.get_scan_bookmark("INBOX", 1) == 3
    assert db.get_imap_message("INBOX", 1, 2)["our_score"] == 1.0
    assert db.get_imap_message("INBOX", 1, 3)["our_score"] == 1.0


def test_oversize_is_terminal_for_bookmark(tmp_path, monkeypatch):
    db = _mk_db(tmp_path)
    acc = _mk_account(mode="shadow")
    with db.tx():
        db.set_scan_bookmark("INBOX", 1, 0)
    monkeypatch.setattr(f, "rspamd_scan_detail", lambda *a, **k: f.ScanResult(1.0, (), None))
    client = CapIMAP(
        existing=_all_existing(),
        uids=[1, 2],
        bodies={1: _raw(1)},
        sizes={1: len(_raw(1)), 2: f.MAX_FETCH_BYTES + 1},
    )

    f.scan_inbox(client, db, LOG, acc, FMAP)

    assert db.get_scan_bookmark("INBOX", 1) == 2
    assert "skipped_oversize" in _events(db)


def test_missing_body_does_not_advance_bookmark(tmp_path, monkeypatch):
    db = _mk_db(tmp_path)
    acc = _mk_account(mode="shadow")
    with db.tx():
        db.set_scan_bookmark("INBOX", 1, 0)
    monkeypatch.setattr(f, "rspamd_scan_detail", lambda *a, **k: f.ScanResult(1.0, (), None))
    client = CapIMAP(
        existing=_all_existing(),
        uids=[1, 2],
        bodies={1: _raw(1)},
        sizes={1: len(_raw(1)), 2: 100},
    )

    f.scan_inbox(client, db, LOG, acc, FMAP)

    assert db.get_scan_bookmark("INBOX", 1) == 1
    assert db.get_imap_message("INBOX", 1, 2) is None


def test_greylist_deferral_stays_in_junk_and_is_retried(tmp_path, monkeypatch):
    """Score 0 from a greylist tempfail must not be rescued to the Inbox."""
    db = _mk_db(tmp_path)
    acc = _mk_account(mode="move", move_grace_seconds=0)
    with db.tx():
        db.set_scan_bookmark("Junk", 1, 0)
    monkeypatch.setattr(
        f, "rspamd_scan_detail",
        lambda *a, **k: f.ScanResult(0.0, (), "soft reject", deferred="greylist"),
    )
    client = CapIMAP(
        existing=_all_existing(),
        uids=[1],
        bodies={1: _raw(1)},
    )

    f.poll_junk(client, db, LOG, acc, FMAP)

    row = db.get_imap_message("Junk", 1, 1)
    assert row is not None
    assert row["our_score"] is None
    assert row["our_action"] is None
    assert db.get_scan_bookmark("Junk", 1) == 0
    events = _events(db)
    assert "scan_deferred" in events
    assert "pending_rescue" not in events
    assert "rescued_to_inbox" not in events
    assert "scan_failed" not in events
