"""Regression tests for the CLAUDE_OPUS5.5_EXTRA_CODE_REVIEW.md fixes.

Run: STATE_DIR=/tmp/x python -m pytest test_opus_review_fixes.py
"""

import datetime as dt
import logging
import os
import tempfile

os.environ.setdefault("STATE_DIR", tempfile.mkdtemp(prefix="sf_test_"))

import pytest  # noqa: E402

import filter as f  # noqa: E402
from test_fetch_discipline import CapIMAP, _body_fetch_uids  # noqa: E402
from test_shadow_mode import (  # noqa: E402
    FMAP,
    RecordingIMAP,
    _all_existing,
    _mk_account,
    _mk_db,
)

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


# ----- OPUS-CR-003: never rescue a Microsoft-flagged spoof ------------------


SPOOF_AR = (
    b"Authentication-Results: mx.microsoft.com; spf=fail smtp.mailfrom=vendor.example;"
    b" dkim=none header.d=none; dmarc=fail action=quarantine"
    b" header.from=vendor.example; compauth=fail reason=000\r\n"
)
FORWARDED_AR = (
    b"Authentication-Results: mx.microsoft.com; spf=fail smtp.mailfrom=vendor.example;"
    b" dkim=fail header.d=vendor.example; dmarc=fail action=none"
    b" header.from=vendor.example; compauth=pass reason=130\r\n"
)


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (SPOOF_AR, True),
        (FORWARDED_AR, False),  # ARC-rescued forward: compauth wins over dmarc
        (b"Authentication-Results: mx.microsoft.com; dmarc=fail header.from=x.example\r\n", True),
        (b"Authentication-Results: mx.microsoft.com; dmarc=pass header.from=x.example\r\n", False),
        (b"Authentication-Results: evil.example; compauth=fail reason=000\r\n", False),
        (b"", False),
    ],
)
def test_m365_spoof_verdict(header, expected):
    assert f.m365_spoof_verdict(header + _raw(4)) is expected


def _allowlisted_junk_setup(tmp_path, mode="move"):
    db = _mk_db(tmp_path)
    acc = _mk_account(
        mode=mode, move_grace_seconds=0, threshold=8.0, actual_name="Rich",
    )
    parsed = f.ParsedPattern("ap@vendor.example", "address")
    with db.tx():
        db.set_scan_bookmark("Junk", 1, 3)
        db.list_upsert_address(
            "person", "Rich", "allow", parsed, source="imap", max_entries=1000,
        )
    return db, acc


def test_allowlisted_spoof_is_not_rescued(tmp_path, monkeypatch):
    db, acc = _allowlisted_junk_setup(tmp_path)
    monkeypatch.setattr(
        f, "rspamd_scan_detail", lambda *a, **k: f.ScanResult(1.0, (), None),
    )
    raw = SPOOF_AR + _raw(4, sender="ap@vendor.example")
    client = CapIMAP(existing=_all_existing(), uids=[4], bodies={4: raw})
    f.poll_junk(client, db, LOG, acc, FMAP)
    assert client.moved == []
    assert "m365_spoof_verdict" in _events(db, "rescue_skipped")[0]
    assert "pending_rescue" not in _events(db)


def test_spoof_is_not_reported_as_would_rescue_in_shadow(tmp_path, monkeypatch):
    db, acc = _allowlisted_junk_setup(tmp_path, mode="shadow")
    monkeypatch.setattr(
        f, "rspamd_scan_detail", lambda *a, **k: f.ScanResult(1.0, (), None),
    )
    raw = SPOOF_AR + _raw(4, sender="ap@vendor.example")
    client = CapIMAP(existing=_all_existing(), uids=[4], bodies={4: raw})
    f.poll_junk(client, db, LOG, acc, FMAP)
    assert "would_rescue" not in _events(db)
    assert _events(db, "rescue_skipped")


def test_allowlisted_compauth_pass_is_still_rescued(tmp_path, monkeypatch):
    db, acc = _allowlisted_junk_setup(tmp_path)
    monkeypatch.setattr(
        f, "rspamd_scan_detail", lambda *a, **k: f.ScanResult(20.0, (), None),
    )
    raw = FORWARDED_AR + _raw(4, sender="ap@vendor.example")
    client = CapIMAP(existing=_all_existing(), uids=[4], bodies={4: raw})
    f.poll_junk(client, db, LOG, acc, FMAP)
    assert client.moved == [([4], "INBOX")]


def test_due_rescue_rechecks_spoof_verdict(tmp_path, monkeypatch):
    db, acc = _allowlisted_junk_setup(tmp_path)
    raw = SPOOF_AR + _raw(4, sender="ap@vendor.example")
    with db.tx():
        db.upsert_imap_message("Junk", 1, 4, message_id="m4@example.com")
        db.update_imap_message("Junk", 1, 4, our_score=1.0, our_action="pending_rescue")
        db.add_pending_move(1, 4, "m4@example.com", folder="Junk")
    client = CapIMAP(existing=_all_existing(), uids=[4], bodies={4: raw})
    f.execute_due_rescues(client, db, LOG, acc, FMAP)
    assert client.moved == []
    assert db.due_pending_moves("Junk", 1, 0) == []
    assert _events(db, "pending_rescue_canceled") == ["m365_spoof_verdict"]


# ----- OPUS-CR-007: user re-junk of a rescued message is learned ------------


def _rescued_then_seen_in_inbox(db, raw):
    sha = f.body_sha256(raw)
    with db.tx():
        db.set_scan_bookmark("Junk", 1, 8)
        db.upsert_imap_message("Junk", 1, 4, message_id="m9@example.com", body_sha256=sha)
        db.update_imap_message(
            "Junk", 1, 4, our_action="rescued_to_inbox", current_folder="INBOX",
        )
        db.upsert_imap_message("INBOX", 1, 50, message_id="m9@example.com", body_sha256=sha)
        db.update_imap_message("INBOX", 1, 50, our_score=1.0)


def test_user_rejunk_of_rescued_message_is_learned(tmp_path, monkeypatch):
    db = _mk_db(tmp_path)
    acc = _mk_account(mode="move", move_grace_seconds=0)
    raw = _raw(9)
    _rescued_then_seen_in_inbox(db, raw)
    monkeypatch.setattr(
        f, "rspamd_scan_detail", lambda *a, **k: pytest.fail("user move is not scanned"),
    )
    client = CapIMAP(existing=_all_existing(), uids=[9], bodies={9: raw})
    f.poll_junk(client, db, LOG, acc, FMAP)
    row = db.get_imap_message("Junk", 1, 9)
    assert row["pending_learn"] == "spam"
    assert client.moved == []


def test_filter_move_of_rescued_message_is_still_not_learned(tmp_path, monkeypatch):
    db = _mk_db(tmp_path)
    acc = _mk_account(mode="move", move_grace_seconds=0)
    raw = _raw(9)
    _rescued_then_seen_in_inbox(db, raw)
    with db.tx():
        db.update_imap_message(
            "INBOX", 1, 50, our_action="moved_to_junk", current_folder="Junk",
        )
    client = CapIMAP(existing=_all_existing(), uids=[9], bodies={9: raw})
    f.poll_junk(client, db, LOG, acc, FMAP)
    row = db.get_imap_message("Junk", 1, 9)
    assert row["pending_learn"] is None
    assert "pending_spam" not in _events(db)


# ----- OPUS-CR-008: do not bounce the user's old mail back out of Junk ------


class _DatedIMAP(CapIMAP):
    def __init__(self, *, dates, **kw):
        super().__init__(**kw)
        self.dates = dict(dates)

    def fetch(self, uids, parts):
        out = super().fetch(uids, parts)
        wanted = [p if isinstance(p, bytes) else str(p).encode() for p in parts]
        if b"INTERNALDATE" in wanted:
            for u in out:
                out[u][b"INTERNALDATE"] = self.dates.get(u)
        return out


@pytest.mark.parametrize(
    ("age", "rescued"),
    [
        (dt.timedelta(days=90), False),
        (dt.timedelta(hours=1), True),
    ],
)
def test_rescue_respects_internaldate_age(tmp_path, monkeypatch, age, rescued):
    db = _mk_db(tmp_path)
    acc = _mk_account(mode="move", move_grace_seconds=0)
    with db.tx():
        db.set_scan_bookmark("Junk", 1, 3)
    monkeypatch.setattr(
        f, "rspamd_scan_detail", lambda *a, **k: f.ScanResult(1.0, (), None),
    )
    client = _DatedIMAP(
        existing=_all_existing(), uids=[4], bodies={4: _raw(4)},
        dates={4: dt.datetime.now() - age},
    )
    f.poll_junk(client, db, LOG, acc, FMAP)
    if rescued:
        assert client.moved == [([4], "INBOX")]
    else:
        assert client.moved == []
        assert "old_internaldate" in _events(db, "rescue_skipped")[0]


# ----- OPUS-CR-009: Junk retention keeps what it must not trash -------------


def _sweep_junk(db, acc, uids):
    client = RecordingIMAP(existing=_all_existing(), search_uids=uids)
    f._sweep_folder_to_trash(
        client, db, LOG, acc, FMAP,
        src=FMAP["junk"], days=10, exclude_learned_ham=True, tag="junk_retention",
    )
    return [u for batch, dest in client.moved if dest == "Trash" for u in batch]


def test_junk_retention_waits_for_poll_junk_bookmark(tmp_path):
    db = _mk_db(tmp_path)
    acc = _mk_account(mode="flag")
    assert _sweep_junk(db, acc, [5, 6]) == []  # no bookmark yet
    with db.tx():
        db.set_scan_bookmark("Junk", 1, 5)
    assert _sweep_junk(db, acc, [5, 6]) == [5]  # 6 not yet seen by poll_junk


def test_junk_retention_skips_pending_allowlisted_and_rescue_rows(tmp_path):
    db = _mk_db(tmp_path)
    acc = _mk_account(mode="flag")
    with db.tx():
        db.set_scan_bookmark("Junk", 1, 20)
        for uid in (11, 12, 13, 14, 15):
            db.upsert_imap_message("Junk", 1, uid, message_id=f"r{uid}@x")
        db.update_imap_message("Junk", 1, 11, pending_learn="spam", pending_learn_at=1)
        db.update_imap_message("Junk", 1, 12, our_action="allowlisted")
        db.update_imap_message("Junk", 1, 13, our_action="pending_rescue")
        db.update_imap_message("Junk", 1, 14, learned_as="ham")
    assert _sweep_junk(db, acc, [11, 12, 13, 14, 15, 16]) == [15, 16]


def test_poll_junk_marks_allowlisted_provider_junk(tmp_path, monkeypatch):
    db, acc = _allowlisted_junk_setup(tmp_path, mode="flag")
    monkeypatch.setattr(
        f, "rspamd_scan_detail", lambda *a, **k: f.ScanResult(12.0, (), None),
    )
    raw = _raw(4, sender="ap@vendor.example")
    client = CapIMAP(existing=_all_existing(), uids=[4], bodies={4: raw})
    f.poll_junk(client, db, LOG, acc, FMAP)
    assert db.get_imap_message("Junk", 1, 4)["our_action"] == "allowlisted"
    assert client.moved == []  # flag mode never rescues
    assert _sweep_junk(db, acc, [4]) == []


def test_canceled_rescue_does_not_stay_pending(tmp_path):
    db = _mk_db(tmp_path)
    acc = _mk_account(mode="move", move_grace_seconds=0, threshold=8.0)
    with db.tx():
        db.upsert_imap_message("Junk", 1, 4, message_id="m4@example.com")
        db.update_imap_message("Junk", 1, 4, our_score=9.0, our_action="pending_rescue")
        db.add_pending_move(1, 4, "m4@example.com", folder="Junk")
    client = CapIMAP(existing=_all_existing(), uids=[4], bodies={4: _raw(4)})
    f.execute_due_rescues(client, db, LOG, acc, FMAP)
    assert client.moved == []
    assert db.get_imap_message("Junk", 1, 4)["our_action"] is None


# ----- OPUS-CR-010: no Message-ID is not a filter bypass --------------------


RAW_NO_MSGID = (
    b"From: spammer@example.net\r\nTo: u@example.com\r\nSubject: win\r\n"
    b"\r\nno message id here\r\n"
)


@pytest.mark.parametrize("mode", ["flag", "move"])
def test_inbox_without_message_id_is_scored_and_routed(tmp_path, monkeypatch, mode):
    db = _mk_db(tmp_path)
    acc = _mk_account(mode=mode, move_grace_seconds=0)
    with db.tx():
        db.set_scan_bookmark("INBOX", 1, 0)
    monkeypatch.setattr(
        f, "rspamd_scan_detail", lambda *a, **k: f.ScanResult(12.0, (), None),
    )
    client = CapIMAP(existing=_all_existing(), uids=[1], bodies={1: RAW_NO_MSGID})
    f.scan_inbox(client, db, LOG, acc, FMAP, f.AccountState())
    row = db.get_imap_message("INBOX", 1, 1)
    assert row["message_id"] is None
    assert row["our_score"] == 12.0
    assert db.get_scan_bookmark("INBOX", 1) == 1
    assert "no_message_id" in _events(db)
    if mode == "flag":
        assert client.flags_added == [(1, [b"\\Flagged"])]
    else:
        f.execute_due_moves(client, db, LOG, acc, FMAP)
        assert client.moved == [([1], "Junk")]
        assert db.get_imap_message("INBOX", 1, 1)["our_action"] == "moved_to_junk"


def test_user_move_without_message_id_is_learned(tmp_path, monkeypatch):
    db = _mk_db(tmp_path)
    acc = _mk_account(mode="shadow")
    with db.tx():
        db.set_scan_bookmark("Junk", 1, 3)
        db.upsert_imap_message(
            "INBOX", 1, 40, message_id=None,
            body_sha256=f.body_sha256(RAW_NO_MSGID),
        )
    client = CapIMAP(existing=_all_existing(), uids=[4], bodies={4: RAW_NO_MSGID})
    f.poll_junk(client, db, LOG, acc, FMAP)
    assert db.get_imap_message("Junk", 1, 4)["pending_learn"] == "spam"


# ----- OPUS-CR-011: transactions take the write lock up front ---------------


def test_tx_holds_write_lock_from_begin(tmp_path):
    import sqlite3

    db = _mk_db(tmp_path)
    other = sqlite3.connect(f.DB_PATH, isolation_level=None, timeout=0)
    try:
        with db.tx():
            db.conn.execute("SELECT COUNT(*) FROM events").fetchone()
            # A concurrent writer cannot slip in between our read and write
            # (with deferred BEGIN it could, and our write would then fail).
            with pytest.raises(sqlite3.OperationalError):
                other.execute(
                    "INSERT INTO events(account, ts, event) VALUES('o', 1, 'x')"
                )
            db.log_event("inside_tx")
        other.execute("INSERT INTO events(account, ts, event) VALUES('o', 1, 'x')")
    finally:
        other.close()
    assert "inside_tx" in _events(db)


# ----- OPUS-CR-012: backoff is not reset by a connect alone -----------------


def test_reconnect_backoff_grows_when_every_pass_fails(tmp_path, monkeypatch):
    db = _mk_db(tmp_path)
    acc = _mk_account(mode="shadow")
    connects: list[int] = []
    sleeps: list[float] = []

    class Client:
        def has_capability(self, _cap):
            return True

        def logout(self):
            return None

    def connect(_acc, timeout=60):
        connects.append(len(sleeps))
        if len(connects) > 3:
            f.SHUTDOWN.set()
            raise OSError("stop test")
        return Client()

    def failing_scan(*_a, **_k):
        raise f.IMAPClientError("STORE failed on one message")

    monkeypatch.setattr(f, "connect_imap", connect)
    monkeypatch.setattr(f, "detect_delimiter", lambda _c: "/")
    monkeypatch.setattr(f, "apply_special_use_remap", lambda *a, **k: None)
    monkeypatch.setattr(f, "ensure_folders", lambda *a, **k: None)
    for name in (
        "drain_train_spam", "drain_train_ham", "drain_list_allow", "drain_list_block",
    ):
        monkeypatch.setattr(f, name, lambda *a, **k: None)
    monkeypatch.setattr(f, "scan_inbox", failing_scan)
    monkeypatch.setattr(f.time, "sleep", lambda s: sleeps.append(s))
    try:
        f._run_account(acc, db)
    finally:
        f.SHUTDOWN.clear()
    gaps = [b - a for a, b in zip(connects, connects[1:])]
    assert gaps == [5, 10, 20]


# ----- OPUS-CR-013: learn budget is checked before fetching bodies ----------


def _spend_learn_budget(db, n):
    with db.tx():
        for _ in range(n):
            db.record_rate("learn")


def test_train_drain_fetches_no_bodies_when_budget_exhausted(tmp_path, monkeypatch):
    db = _mk_db(tmp_path)
    acc = _mk_account(max_learns_per_hour=3)
    _spend_learn_budget(db, 3)
    monkeypatch.setattr(f, "rspamd_learn", lambda *a, **k: pytest.fail("no learn"))
    client = CapIMAP(
        existing=_all_existing(), uids=[1, 2, 3],
        bodies={n: _raw(n) for n in (1, 2, 3)},
    )
    f.drain_train_spam(client, db, LOG, acc, FMAP)
    assert _body_fetch_uids(client) == []
    assert client.moved == []


def test_train_drain_fetches_only_what_budget_allows(tmp_path, monkeypatch):
    db = _mk_db(tmp_path)
    acc = _mk_account(max_learns_per_hour=5)
    _spend_learn_budget(db, 3)
    monkeypatch.setattr(f, "rspamd_learn", lambda *a, **k: "learned")
    client = CapIMAP(
        existing=_all_existing(), uids=[1, 2, 3, 4, 5],
        bodies={n: _raw(n) for n in range(1, 6)},
    )
    f.drain_train_spam(client, db, LOG, acc, FMAP)
    assert _body_fetch_uids(client) == [1, 2]
    assert client.moved == [([1, 2], FMAP["trained_spam"])]


def test_pending_learns_defer_without_fetch_when_budget_exhausted(tmp_path, monkeypatch):
    db = _mk_db(tmp_path)
    acc = _mk_account(max_learns_per_hour=2, learn_grace_seconds=0)
    _spend_learn_budget(db, 2)
    with db.tx():
        for uid in (7, 8):
            db.upsert_imap_message("Junk", 1, uid, message_id=f"p{uid}@x")
            db.update_imap_message(
                "Junk", 1, uid, pending_learn="spam", pending_learn_at=1,
            )
    monkeypatch.setattr(f, "rspamd_learn", lambda *a, **k: pytest.fail("no learn"))
    client = CapIMAP(
        existing=_all_existing(), uids=[7, 8], bodies={7: _raw(7), 8: _raw(8)},
    )
    f.process_pending_learns(client, db, LOG, acc, FMAP)
    assert _body_fetch_uids(client) == []
    for uid in (7, 8):
        row = db.get_imap_message("Junk", 1, uid)
        assert row["pending_learn"] == "spam"
        assert row["learn_retry_count"] == 1


# ----- OPUS-CR-014: a Train-* leftover copy is never re-moved --------------


def test_train_leftover_after_move_as_copy_is_not_moved_twice(tmp_path, monkeypatch):
    db = _mk_db(tmp_path)
    acc = _mk_account()
    monkeypatch.setattr(f, "rspamd_learn", lambda *a, **k: "learned")
    client = RecordingIMAP(
        existing=_all_existing(),
        search_uids=[1],
        fetch_by_uid={1: {b"BODY[]": _raw(1), b"FLAGS": ()}},
        move_leaves_copies=True,
    )
    f.drain_train_spam(client, db, LOG, acc, FMAP)
    assert client.moved == [([1], FMAP["trained_spam"])]
    for _ in range(3):
        f.drain_train_spam(client, db, LOG, acc, FMAP)
    assert client.moved == [([1], FMAP["trained_spam"])]
    assert client.expunged == []  # never deletes the leftover
