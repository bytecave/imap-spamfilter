"""Coverage for IMAP-CR-001 through IMAP-CR-019 production fixes."""

from __future__ import annotations

import json
import logging
import os
import stat
import tempfile
import time
from pathlib import Path

os.environ.setdefault("STATE_DIR", tempfile.mkdtemp(prefix="sf_cr_"))

import pytest  # noqa: E402

import filter as f  # noqa: E402
from test_address_lists import _scan_client, _seed  # noqa: E402
from test_connection import _write_accounts  # noqa: E402
from test_shadow_mode import FMAP, LOG, RAW_SCAN, RecordingIMAP, _all_existing  # noqa: E402
from test_shadow_mode import _mk_account, _mk_db  # noqa: E402


def _queue_due_pending(db, *, uid=1, msgid="<scan1@example.com>", sender="sender@example.com"):
    with db.tx():
        db.upsert_imap_message(
            "INBOX", 1, uid,
            message_id=msgid, sender=sender, subject="suspicious offer",
            body_sha256="deadbeef",
        )
        db.update_imap_message(
            "INBOX", 1, uid, our_action="pending_move", our_score=9.0,
        )
        db.add_pending_move(1, uid, msgid, folder="INBOX")
        db.conn.execute(
            "UPDATE pending_move SET flag_at=? WHERE account=? AND uid=?",
            (int(time.time()) - 120, db.account, uid),
        )


def _pending_count(db) -> int:
    return db.conn.execute("SELECT COUNT(*) FROM pending_move").fetchone()[0]


def test_allow_cancels_scored_pending_move(tmp_path, monkeypatch):
    db = _mk_db(tmp_path)
    acc = _mk_account(mode="move", actual_name="Rich", move_grace_seconds=60)
    _queue_due_pending(db)
    _seed(db, "person", "Rich", "allow", "sender@example.com")
    with db.tx():
        db.set_scan_bookmark("INBOX", 1, 0)
    monkeypatch.setattr(
        f, "rspamd_scan_detail",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no scan")),
    )
    client = _scan_client()
    f.scan_inbox(client, db, LOG, acc, FMAP)
    assert _pending_count(db) == 0
    assert db.get_imap_message("INBOX", 1, 1)["our_action"] == "allowlisted"
    client.moved = []
    f.execute_due_moves(client, db, LOG, acc, FMAP)
    assert client.moved == []
    assert db.get_imap_message("INBOX", 1, 1)["current_folder"] == "INBOX"


def test_user_allow_beats_block_pending_move(tmp_path):
    db = _mk_db(tmp_path)
    acc = _mk_account(mode="move", actual_name="Rich", move_grace_seconds=60)
    _queue_due_pending(db)
    _seed(db, "person", "Rich", "block", "sender@example.com")
    _seed(db, "person", "Rich", "allow", "sender@example.com")
    client = RecordingIMAP(
        existing=_all_existing(),
        fetch_by_uid={1: {b"BODY[]": RAW_SCAN, b"FLAGS": ()}},
    )
    f.execute_due_moves(client, db, LOG, acc, FMAP)
    assert client.moved == []
    assert _pending_count(db) == 0
    assert db.get_imap_message("INBOX", 1, 1)["our_action"] == "allowlisted"


def test_sender_only_allow_cancels_due_move(tmp_path):
    db = _mk_db(tmp_path)
    acc = _mk_account(mode="move", actual_name="Rich", move_grace_seconds=60)
    raw = (
        b"From: other@y.com\r\n"
        b"Sender: Safe@X.com\r\n"
        b"Subject: hi\r\n"
        b"Message-ID: <snd@example.com>\r\n"
        b"\r\nbody\r\n"
    )
    _queue_due_pending(db, msgid="<snd@example.com>", sender="other@y.com")
    _seed(db, "person", "Rich", "allow", "safe@x.com")
    client = RecordingIMAP(
        existing=_all_existing(),
        fetch_by_uid={1: {b"BODY[]": raw, b"FLAGS": ()}},
    )
    f.execute_due_moves(client, db, LOG, acc, FMAP)
    assert client.moved == []
    assert db.get_imap_message("INBOX", 1, 1)["our_action"] == "allowlisted"


def test_allow_added_after_due_rows_loaded_skips_move(tmp_path):
    db = _mk_db(tmp_path)
    acc = _mk_account(mode="move", actual_name="Rich", move_grace_seconds=60)
    _queue_due_pending(db)
    real_due = db.due_pending_moves

    def wrapped(*a, **k):
        rows = real_due(*a, **k)
        _seed(db, "person", "Rich", "allow", "sender@example.com")
        return rows

    db.due_pending_moves = wrapped
    client = RecordingIMAP(
        existing=_all_existing(),
        fetch_by_uid={1: {b"BODY[]": RAW_SCAN, b"FLAGS": ()}},
    )
    f.execute_due_moves(client, db, LOG, acc, FMAP)
    assert client.moved == []
    assert _pending_count(db) == 0


def test_removing_allow_does_not_cancel_valid_pending_move(tmp_path):
    db = _mk_db(tmp_path)
    acc = _mk_account(mode="move", actual_name="Rich", move_grace_seconds=60)
    _queue_due_pending(db)
    _seed(db, "person", "Rich", "allow", "unrelated@x.com")
    with db.tx():
        db.conn.execute(
            "DELETE FROM address_lists WHERE pattern=?",
            ("unrelated@x.com",),
        )
    client = RecordingIMAP(
        existing=_all_existing(),
        fetch_by_uid={1: {b"BODY[]": RAW_SCAN, b"FLAGS": ()}},
    )
    f.execute_due_moves(client, db, LOG, acc, FMAP)
    assert client.moved == [([1], "Junk")]
    assert db.get_imap_message("INBOX", 1, 1)["our_action"] == "moved_to_junk"


def test_prune_keeps_inbox_fingerprint_for_later_junk_learn(tmp_path, monkeypatch):
    db = _mk_db(tmp_path)
    acc = _mk_account()
    raw = RAW_SCAN
    sha = f.body_sha256(raw)
    old = int(time.time()) - 40 * 86400
    with db.tx():
        db.upsert_imap_message(
            "INBOX", 1, 1,
            message_id="<scan1@example.com>", sender="sender@example.com",
            subject="suspicious offer", body_sha256=sha,
        )
        db.conn.execute(
            "UPDATE messages SET last_seen=?, first_seen=? WHERE uid=1",
            (old, old),
        )
        deleted = db.prune_messages(21 * 86400, inbox="INBOX")
    assert deleted == 1
    assert db.get_imap_message("INBOX", 1, 1) is None
    assert db.get_inbox_fingerprint(sha, "INBOX") is not None

    with db.tx():
        db.set_scan_bookmark("Junk", 1, 0)
    learned = {"n": 0}
    monkeypatch.setattr(
        f, "rspamd_learn",
        lambda *a, **k: learned.__setitem__("n", learned["n"] + 1) or "learned",
    )
    from test_core_review_fixes import _FlagsIMAP
    client = _FlagsIMAP(
        existing=_all_existing(), uids=[2], bodies={2: raw},
        flags={2: (b"$Junk",)},
    )
    f.poll_junk(client, db, LOG, acc, FMAP)
    assert learned["n"] == 1
    assert db.get_inbox_fingerprint(sha, "INBOX") is None


def test_fingerprint_growth_is_capped(tmp_path):
    db = _mk_db(tmp_path)
    now = int(time.time())
    with db.tx():
        for i in range(5):
            db.conn.execute(
                """
                INSERT INTO message_fingerprints(
                    account, body_sha256, folder, uidvalidity, uid,
                    first_seen, last_seen, learned_as
                ) VALUES (?,?,?,?,?,?,?,?)
                """,
                (db.account, f"sha{i}", "INBOX", 1, i, now - i, now - i, None),
            )
        removed = db.prune_fingerprints(max_age_days=400, max_rows=3)
    assert removed == 2
    left = db.conn.execute(
        "SELECT COUNT(*) FROM message_fingerprints WHERE account=?",
        (db.account,),
    ).fetchone()[0]
    assert left == 3


@pytest.mark.parametrize("field", [
    "name", "user", "password", "imap_host", "actual_name", "inbox",
])
@pytest.mark.parametrize("bad", [123, True, [], {}, None, "", "   "])
def test_required_strings_reject_wrong_yaml_types(tmp_path, field, bad):
    import yaml
    path = _write_accounts(tmp_path, "")
    raw = yaml.safe_load(path.read_text())
    if field in raw["accounts"][0] or field == "inbox":
        if field == "inbox":
            raw.setdefault("defaults", {})["inbox"] = bad
        else:
            raw["accounts"][0][field] = bad
    else:
        raw["accounts"][0][field] = bad
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(f.ConfigError, match=field):
        f.load_accounts(path)


def test_init_db_sets_private_modes(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    os.chmod(state, 0o755)
    db_path = state / "spamfilter.db"
    monkeypatch.setattr(f, "STATE_DIR", state)
    monkeypatch.setattr(f, "DB_PATH", db_path)
    old = os.umask(0o022)
    try:
        f.init_db()
        db = f.Db("acct")
        db.touch_heartbeat_ok()
        db.close()
    finally:
        os.umask(old)
    assert stat.S_IMODE(state.stat().st_mode) == 0o700
    assert stat.S_IMODE(db_path.stat().st_mode) == 0o600
    wal = Path(str(db_path) + "-wal")
    shm = Path(str(db_path) + "-shm")
    for sidecar in (wal, shm):
        if sidecar.exists():
            assert stat.S_IMODE(sidecar.stat().st_mode) == 0o600


def test_score_detail_json_caps_huge_action_and_unicode():
    huge = "A" * 50_000
    uni = "€" * 20_000
    result = f.ScanResult(
        score=9.0,
        action=huge,
        symbols=(f.ScanSymbol(uni, 1.0, uni),) * 20,
    )
    text = f.score_detail_json(result)
    assert len(text.encode("utf-8")) <= f.SCORE_DETAIL_MAX_BYTES
    payload = json.loads(text)
    assert payload["score"] == 9.0
    assert isinstance(payload["symbols"], list)


def test_list_write_rejects_invalid_enums_and_rolls_back(tmp_path):
    db = _mk_db(tmp_path)
    parsed = f.ParsedPattern("a@x.com", "address")
    with db.tx():
        db.list_upsert_address(
            "person", "Rich", "allow", parsed,
            source="dashboard", max_entries=10,
        )
    with pytest.raises(ValueError, match="scope_type"):
        with db.tx():
            db.list_upsert_address(
                "nope", "Rich", "allow", parsed,
                source="dashboard", max_entries=10,
            )
    with pytest.raises(ValueError, match="kind"):
        with db.tx():
            db.list_replace(
                "person", "Rich", "maybe", [parsed],
                source="dashboard", actor="t", max_entries=10,
            )
    with pytest.raises(ValueError, match="source"):
        with db.tx():
            db.list_upsert_address(
                "person", "Rich", "allow", parsed,
                source="ftp", max_entries=10,
            )
    assert db.list_get("person", "Rich", "allow") == ["a@x.com"]
    # Person-scoped domain patterns remain allowed.
    host = f.ParsedPattern("@vendor.com", "domain")
    with db.tx():
        assert db.list_upsert_address(
            "person", "Rich", "allow", host,
            source="imap", max_entries=10,
        ) == "inserted"
    assert "@vendor.com" in db.list_get("person", "Rich", "allow")


def test_readme_rate_limit_contract_matches_builtins():
    readme = Path(__file__).resolve().parents[1] / "README.md"
    text = readme.read_text()
    assert "does **not** enter safe-mode" in text
    assert "cap per `drain_train_spam` / `drain_train_ham` run" in text
    for key in (
        "max_moves_per_hour",
        "max_learns_per_hour",
        "max_train_per_run",
        "max_list_per_run",
        "max_list_entries",
        "safe_mode_unseen_cap",
    ):
        assert f"`{key}`" in text
        assert str(f.BUILTIN_DEFAULTS[key]) in text
    assert "400 days" in text


def test_flask_pin_is_patched():
    from importlib.metadata import version
    parts = tuple(int(p) for p in version("flask").split(".")[:3])
    assert parts >= (3, 1, 3)
    req = (Path(__file__).resolve().parent / "requirements.txt").read_text()
    assert "Flask==3.1.3" in req
