"""MULTI_TRAIN: prior spam learns of one From address, in this mailbox.

A raw rspamd score at or below half of rescue_below is left alone. Above
that, 1/2/3 prior trains add +1/+2/+4, and 4 or more force Junk routing
without changing the rspamd number and without teaching Bayes.
"""

import json
import logging
import os
import tempfile

os.environ.setdefault("STATE_DIR", tempfile.mkdtemp(prefix="sf_test_"))

import filter as f  # noqa: E402
from test_fetch_discipline import CapIMAP  # noqa: E402
from test_shadow_mode import FMAP, _all_existing, _mk_account, _mk_db  # noqa: E402

LOG = logging.getLogger("test")
SENDER = "noreply@aboco.example"


def _raw(sender: str, n: int = 1) -> bytes:
    return (
        f"From: {sender}\r\nTo: u@example.com\r\nSubject: m{n}\r\n"
        f"Message-ID: <new-{n}@example.com>\r\n\r\nbody {n}\r\n"
    ).encode()


def _seed(db, n: int, sender: str = SENDER, *, learned_as: str = "spam",
          message_id: str | None = "auto", body_sha256: str | None = "auto"):
    mid = f"train-{n}@example.com" if message_id == "auto" else message_id
    digest = f"body-{n}" if body_sha256 == "auto" else body_sha256
    with db.tx():
        db.upsert_imap_message(
            "Junk/Trained-Spam", 1, 1000 + n,
            message_id=mid, sender=sender, subject="trained",
            body_sha256=digest,
        )
        db.update_imap_message(
            "Junk/Trained-Spam", 1, 1000 + n, learned_as=learned_as,
        )


def _scan(db, monkeypatch, raw: bytes, score: float, *, mode: str = "move"):
    acc = _mk_account(mode=mode, move_grace_seconds=0)
    with db.tx():
        db.set_scan_bookmark("INBOX", 1, 0)
    monkeypatch.setattr(
        f, "rspamd_scan_detail", lambda *a, **k: f.ScanResult(score, (), None),
    )
    client = CapIMAP(existing=_all_existing(), uids=[1], bodies={1: raw})
    f.scan_inbox(client, db, LOG, acc, FMAP)
    return db.get_imap_message("INBOX", 1, 1)


def _pending(db, folder="INBOX"):
    return db.conn.execute(
        "SELECT uid FROM pending_move WHERE account=? AND folder=?",
        (db.account, folder),
    ).fetchall()


def _detail(row):
    return json.loads(row["score_detail"])


def _db(tmp_path, name: str):
    sub = tmp_path / name
    sub.mkdir()
    return _mk_db(sub)


def test_ladder_and_gate(tmp_path, monkeypatch):
    assert f.multi_train_for_count(0) == f.MultiTrainAdjustment(0.0, False)
    assert f.multi_train_for_count(1) == f.MultiTrainAdjustment(1.0, False)
    assert f.multi_train_for_count(2) == f.MultiTrainAdjustment(2.0, False)
    assert f.multi_train_for_count(3) == f.MultiTrainAdjustment(4.0, False)
    assert f.multi_train_for_count(4).force is True
    assert f.multi_train_for_count(9).force is True

    db = _db(tmp_path, "zero")
    raw = _raw(SENDER)
    row = _scan(db, monkeypatch, raw, 4.74)
    assert row["our_score"] == 4.74
    assert _pending(db) == []

    for n, added in ((1, 1.0), (2, 2.0), (3, 4.0)):
        db = _db(tmp_path, f"n{n}")
        for i in range(1, n + 1):
            _seed(db, i)
        row = _scan(db, monkeypatch, raw, 4.74)
        assert row["our_score"] == 4.74 + added
        detail = _detail(row)
        assert detail["symbols"][0]["n"] == "MULTI_TRAIN"
        assert detail["symbols"][0]["s"] == added
        assert "multi_train" not in detail


def test_three_trains_queue_junk_and_two_do_not(tmp_path, monkeypatch):
    db = _db(tmp_path, "two")
    for i in range(1, 3):
        _seed(db, i)
    row = _scan(db, monkeypatch, _raw(SENDER), 4.74)
    assert row["our_score"] == 4.74 + 2
    assert _pending(db) == []

    db = _db(tmp_path, "three")
    for i in range(1, 4):
        _seed(db, i)
    row = _scan(db, monkeypatch, _raw(SENDER), 4.74)
    assert row["our_score"] == 4.74 + 4
    assert [r["uid"] for r in _pending(db)] == [1]
    assert row["our_action"] == "pending_move"
    assert row["learned_as"] is None


def test_four_trains_force_above_gate_and_spare_at_or_below(tmp_path, monkeypatch):
    db = _db(tmp_path, "force")
    for i in range(1, 5):
        _seed(db, i)
    row = _scan(db, monkeypatch, _raw(SENDER), 3.0)
    assert row["our_score"] == 3.0
    assert _detail(row)["multi_train"] == "force"
    assert _detail(row)["symbols"][0]["n"] == "MULTI_TRAIN"
    assert _detail(row)["symbols"][0]["s"] == 0.0
    assert [r["uid"] for r in _pending(db)] == [1]
    assert row["learned_as"] is None

    db = _db(tmp_path, "gate")
    for i in range(1, 5):
        _seed(db, i)
    row = _scan(db, monkeypatch, _raw(SENDER), 2.0)
    assert row["our_score"] == 2.0
    assert "MULTI_TRAIN" not in (row["score_detail"] or "")
    assert _pending(db) == []

    db = _db(tmp_path, "ham")
    for i in range(1, 5):
        _seed(db, i)
    row = _scan(db, monkeypatch, _raw(SENDER), -25.0)
    assert row["our_score"] == -25.0
    assert _pending(db) == []


def test_same_message_id_counts_once_and_scopes_stay_separate(tmp_path, monkeypatch):
    db = _db(tmp_path, "dup")
    _seed(db, 1, message_id="same@example.com")
    _seed(db, 2, message_id="same@example.com")
    assert f.spam_train_count(
        db, SENDER, message_id="new@example.com", body_sha256="fresh",
    ) == 1
    row = _scan(db, monkeypatch, _raw(SENDER), 4.74)
    assert row["our_score"] == 4.74 + 1

    db = _db(tmp_path, "hash")
    _seed(db, 1, message_id=None, body_sha256="hash-a")
    _seed(db, 2, message_id=None, body_sha256="hash-a")
    _seed(db, 3, message_id=None, body_sha256="hash-b")
    assert f.spam_train_count(
        db, SENDER, message_id=None, body_sha256="other",
    ) == 2
    assert f.spam_train_count(
        db, SENDER, message_id=None, body_sha256="hash-a",
    ) == 1

    db = _db(tmp_path, "other")
    now = 1
    with db.tx():
        for i in range(1, 5):
            db.conn.execute(
                """
                INSERT INTO messages(
                    account, folder, uidvalidity, uid, message_id, body_sha256,
                    first_seen, last_seen, current_folder, sender, learned_as
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    "other", "Junk", 1, i, f"o{i}@example.com", f"ob{i}",
                    now, now, "Junk", SENDER, "spam",
                ),
            )
    row = _scan(db, monkeypatch, _raw(SENDER), 4.74)
    assert row["our_score"] == 4.74
    assert _pending(db) == []

    db = _db(tmp_path, "domain")
    for i in range(1, 5):
        _seed(db, i, sender="sales@aboco.example")
    row = _scan(db, monkeypatch, _raw(SENDER), 3.0)
    assert row["our_score"] == 3.0
    assert _pending(db) == []

    db = _db(tmp_path, "case")
    for i in range(1, 5):
        _seed(db, i, sender="INFO@aboco.example")
    row = _scan(db, monkeypatch, _raw("info@aboco.example"), 3.0)
    assert _detail(row)["multi_train"] == "force"


def test_ham_learn_drops_a_force_back_to_plus_four(tmp_path, monkeypatch):
    db = _db(tmp_path, "ham")
    for i in range(1, 5):
        _seed(db, i)
    with db.tx():
        db.update_imap_message(
            "Junk/Trained-Spam", 1, 1001, learned_as="ham",
        )
    row = _scan(db, monkeypatch, _raw(SENDER), 4.74)
    assert row["our_score"] == 4.74 + 4
    assert "multi_train" not in _detail(row)
    assert [r["uid"] for r in _pending(db)] == [1]


def test_allow_list_holds_a_forced_message(tmp_path, monkeypatch):
    db = _db(tmp_path, "allow")
    acc = _mk_account(mode="move", move_grace_seconds=0)
    for i in range(1, 5):
        _seed(db, i)
    with db.tx():
        db.set_scan_bookmark("INBOX", 1, 0)
        db.list_upsert_address(
            "person", acc.actual_name, "allow",
            f.ParsedPattern(pattern=SENDER, pattern_type="address"),
            source="dashboard", actor="test", max_entries=1000,
        )
    monkeypatch.setattr(
        f, "rspamd_scan_detail", lambda *a, **k: f.ScanResult(3.0, (), None),
    )
    client = CapIMAP(
        existing=_all_existing(), uids=[1], bodies={1: _raw(SENDER)},
    )
    f.scan_inbox(client, db, LOG, acc, FMAP)
    row = db.get_imap_message("INBOX", 1, 1)
    assert row["our_action"] == "allowlisted"
    assert row["our_score"] == 3.0
    assert _detail(row)["multi_train"] == "force"
    assert _pending(db) == []


def test_junk_rescue_respects_the_gate(tmp_path, monkeypatch):
    def _poll(score: float):
        db = _db(tmp_path, f"score-{score}")
        acc = _mk_account(mode="move", move_grace_seconds=0)
        for i in range(1, 5):
            _seed(db, i)
        with db.tx():
            db.set_scan_bookmark("Junk", 1, 3)
        monkeypatch.setattr(
            f, "rspamd_scan_detail", lambda *a, **k: f.ScanResult(score, (), None),
        )
        client = CapIMAP(
            existing=_all_existing(), uids=[4], bodies={4: _raw(SENDER, 4)},
        )
        f.poll_junk(client, db, LOG, acc, FMAP)
        row = db.get_imap_message("Junk", 1, 4)
        events = [
            r["event"] for r in db.conn.execute("SELECT event FROM events")
        ]
        return row, events

    low, low_events = _poll(1.5)
    assert low["our_score"] == 1.5
    assert "pending_rescue" in low_events
    assert "multi_train" not in (low["score_detail"] or "")

    forced, forced_events = _poll(3.0)
    assert forced["our_score"] == 3.0
    assert _detail(forced)["multi_train"] == "force"
    assert "pending_rescue" not in forced_events
    assert "would_rescue" not in forced_events

    spared, spared_events = _poll(-25.0)
    assert spared["our_score"] == -25.0
    assert "pending_rescue" in spared_events
