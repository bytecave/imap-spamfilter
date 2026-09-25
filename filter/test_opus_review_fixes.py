"""Regression tests for the CLAUDE_OPUS5.5_EXTRA_CODE_REVIEW.md fixes.

Run: STATE_DIR=/tmp/x python -m pytest test_opus_review_fixes.py
"""

import logging
import os
import tempfile

os.environ.setdefault("STATE_DIR", tempfile.mkdtemp(prefix="sf_test_"))

import pytest  # noqa: E402

import filter as f  # noqa: E402
from test_fetch_discipline import CapIMAP  # noqa: E402
from test_shadow_mode import FMAP, _all_existing, _mk_account, _mk_db  # noqa: E402

LOG = logging.getLogger("test")


def _raw(n: int, *, sender: str | None = None, extra: bytes = b"") -> bytes:
    frm = sender or f"s{n}@example.com"
    return (
        f"From: {frm}\r\nTo: u@example.com\r\nSubject: m{n}\r\n"
        f"Message-ID: <m{n}@example.com>\r\n".encode()
        + extra
        + f"\r\nbody {n}\r\n".encode()
    )


def _events(db, name=None):
    rows = db.conn.execute("SELECT event, detail FROM events ORDER BY id").fetchall()
    if name is None:
        return [r["event"] for r in rows]
    return [r["detail"] for r in rows if r["event"] == name]


# ----- OPUS-CR-002: poison message give-up ----------------------------------


@pytest.fixture
def fast_poison(monkeypatch):
    monkeypatch.setattr(f, "SCAN_POISON_ATTEMPTS", 3)
    monkeypatch.setattr(f, "SCAN_POISON_MIN_AGE_S", 0)


def _scan_fails_for(marker: bytes, *, probe_ok: bool = True):
    def scan(raw, *a, **k):
        if raw == f._RSPAMD_PROBE_RAW:
            return f.ScanResult(1.0, (), None) if probe_ok else None
        if marker in raw:
            return None
        return f.ScanResult(1.0, (), None)
    return scan


def test_poison_inbox_uid_is_given_up_and_later_mail_scored(
        tmp_path, monkeypatch, fast_poison):
    db = _mk_db(tmp_path)
    acc = _mk_account(mode="shadow")
    with db.tx():
        db.set_scan_bookmark("INBOX", 1, 1)
    monkeypatch.setattr(f, "rspamd_scan_detail", _scan_fails_for(b"<m2@"))
    client = CapIMAP(
        existing=_all_existing(), uids=[1, 2, 3],
        bodies={n: _raw(n) for n in (1, 2, 3)},
    )
    state = f.AccountState()
    for _ in range(2):
        f.scan_inbox(client, db, LOG, acc, FMAP, state)
    assert db.get_scan_bookmark("INBOX", 1) == 1  # still retrying
    f.scan_inbox(client, db, LOG, acc, FMAP, state)
    assert db.get_scan_bookmark("INBOX", 1) == 3
    assert db.get_imap_message("INBOX", 1, 2)["our_action"] == "scan_giveup"
    assert db.get_imap_message("INBOX", 1, 3)["our_score"] == 1.0
    assert _events(db, "scan_giveup")
    # Catch-up must not keep re-scanning the given-up row.
    assert 2 not in db.unscored_inbox_uids("INBOX", 1, 50)


def test_rspamd_outage_never_gives_up(tmp_path, monkeypatch, fast_poison):
    db = _mk_db(tmp_path)
    acc = _mk_account(mode="shadow")
    with db.tx():
        db.set_scan_bookmark("INBOX", 1, 1)
    monkeypatch.setattr(
        f, "rspamd_scan_detail", _scan_fails_for(b"<m2@", probe_ok=False),
    )
    client = CapIMAP(
        existing=_all_existing(), uids=[2, 3],
        bodies={n: _raw(n) for n in (2, 3)},
    )
    state = f.AccountState()
    for _ in range(10):
        f.scan_inbox(client, db, LOG, acc, FMAP, state)
    assert db.get_scan_bookmark("INBOX", 1) == 1
    assert _events(db, "scan_giveup") == []


def test_without_state_scan_failure_keeps_historic_halt(
        tmp_path, monkeypatch, fast_poison):
    db = _mk_db(tmp_path)
    acc = _mk_account(mode="shadow")
    with db.tx():
        db.set_scan_bookmark("INBOX", 1, 1)
    monkeypatch.setattr(f, "rspamd_scan_detail", _scan_fails_for(b"<m2@"))
    client = CapIMAP(
        existing=_all_existing(), uids=[2, 3],
        bodies={n: _raw(n) for n in (2, 3)},
    )
    for _ in range(5):
        f.scan_inbox(client, db, LOG, acc, FMAP)
    assert db.get_scan_bookmark("INBOX", 1) == 1


def test_repeated_empty_body_is_given_up_without_probe(
        tmp_path, monkeypatch, fast_poison):
    db = _mk_db(tmp_path)
    acc = _mk_account(mode="shadow")
    with db.tx():
        db.set_scan_bookmark("INBOX", 1, 1)
    probes = []

    def scan(raw, *a, **k):
        if raw == f._RSPAMD_PROBE_RAW:
            probes.append(1)
        return f.ScanResult(1.0, (), None)

    monkeypatch.setattr(f, "rspamd_scan_detail", scan)
    client = CapIMAP(
        existing=_all_existing(), uids=[2, 3],
        bodies={3: _raw(3)}, sizes={2: 100},
    )
    state = f.AccountState()
    for _ in range(3):
        f.scan_inbox(client, db, LOG, acc, FMAP, state)
    assert db.get_scan_bookmark("INBOX", 1) == 3
    assert probes == []
    assert "reason=no_body" in _events(db, "scan_giveup")[0]


def test_poison_junk_uid_does_not_block_later_user_move_learn(
        tmp_path, monkeypatch, fast_poison):
    db = _mk_db(tmp_path)
    acc = _mk_account(mode="shadow")
    user_moved = _raw(5)
    with db.tx():
        db.set_scan_bookmark("Junk", 1, 3)
        db.upsert_imap_message(
            "INBOX", 1, 40, message_id="m5@example.com",
            body_sha256=f.body_sha256(user_moved),
        )
    monkeypatch.setattr(f, "rspamd_scan_detail", _scan_fails_for(b"<m4@"))
    client = CapIMAP(
        existing=_all_existing(), uids=[4, 5],
        bodies={4: _raw(4), 5: user_moved},
    )
    state = f.AccountState()
    for _ in range(3):
        f.poll_junk(client, db, LOG, acc, FMAP, state)
    assert db.get_scan_bookmark("Junk", 1) == 5
    assert db.get_imap_message("Junk", 1, 4)["our_action"] == "scan_giveup"
    assert db.get_imap_message("Junk", 1, 5)["pending_learn"] == "spam"
