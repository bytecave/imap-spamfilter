"""Allow/block list core: loader, parser, match, scan routing.

Run: STATE_DIR=/tmp/x python -m pytest test_address_lists.py
"""

import logging
import os
import tempfile
from pathlib import Path

os.environ.setdefault("STATE_DIR", tempfile.mkdtemp(prefix="sf_test_"))

import pytest  # noqa: E402

import filter as f  # noqa: E402
from test_connection import _write_accounts  # noqa: E402
from test_shadow_mode import FMAP, LOG, RAW_SCAN, _mk_account, _mk_db  # noqa: E402
from test_shadow_mode import RecordingIMAP, _all_existing  # noqa: E402


def _seed(db, scope_type, scope_key, kind, pattern, pattern_type="address"):
    parsed = f.ParsedPattern(pattern=pattern, pattern_type=pattern_type)
    with db.tx():
        outcome = db.list_upsert_address(
            scope_type, scope_key, kind, parsed,
            source="dashboard", actor="test", max_entries=1000,
        )
    assert outcome == "inserted"


def test_load_accounts_requires_actual_name(tmp_path):
    path = tmp_path / "accounts.yml"
    path.write_text(
        "accounts:\n"
        "  - name: a\n"
        "    imap_host: imap.example.com\n"
        "    user: u@example.com\n"
        "    password: \"x\"\n"
    )
    with pytest.raises(SystemExit, match="actual_name"):
        f.load_accounts(path)


def test_load_accounts_preserves_actual_name_case(tmp_path):
    accs = f.load_accounts(_write_accounts(tmp_path, ""))
    assert accs[0].actual_name == "Tester"


def test_list_domains_parsed_and_lowercased(tmp_path):
    text = (
        "list_domains:\n"
        "  - domain: ByteCave.NET\n"
        "    type: company\n"
        "  - domain: eizenhoefer.net\n"
        "    type: personal\n"
        "accounts:\n"
        "  - name: a\n"
        "    imap_host: h\n"
        "    user: u@bytecave.net\n"
        "    password: x\n"
        "    actual_name: Rich Eizenhoefer\n"
    )
    path = tmp_path / "accounts.yml"
    path.write_text(text)
    accs = f.load_accounts(path)
    assert accs[0].list_roster.has_domain("bytecave.net")
    assert accs[0].list_roster.domain_type("BYTECAVE.net") == "company"
    assert accs[0].actual_name == "Rich Eizenhoefer"


def test_list_domains_rejects_bad_type_and_duplicate(tmp_path):
    path = _write_accounts(tmp_path, "")
    path.write_text(
        "list_domains:\n"
        "  - domain: a.com\n"
        "    type: nope\n"
        "accounts:\n"
        "  - name: a\n"
        "    imap_host: h\n"
        "    user: u@a.com\n"
        "    password: x\n"
        "    actual_name: A\n"
    )
    with pytest.raises(SystemExit, match="company"):
        f.load_accounts(path)

    path.write_text(
        "list_domains:\n"
        "  - domain: a.com\n"
        "    type: company\n"
        "  - domain: A.com\n"
        "    type: personal\n"
        "accounts:\n"
        "  - name: a\n"
        "    imap_host: h\n"
        "    user: u@a.com\n"
        "    password: x\n"
        "    actual_name: A\n"
    )
    with pytest.raises(SystemExit, match="duplicate"):
        f.load_accounts(path)


def test_unknown_list_domain_key_rejected(tmp_path):
    path = tmp_path / "accounts.yml"
    path.write_text(
        "list_domains:\n"
        "  - domain: a.com\n"
        "    type: company\n"
        "    extra: 1\n"
        "accounts:\n"
        "  - name: a\n"
        "    imap_host: h\n"
        "    user: u@a.com\n"
        "    password: x\n"
        "    actual_name: A\n"
    )
    with pytest.raises(SystemExit, match="unknown key"):
        f.load_accounts(path)


def test_parse_list_line_and_text():
    assert f.parse_list_line("  A@X.com  ", allow_domain=False).pattern == "a@x.com"
    assert f.parse_list_line("", allow_domain=False) is None
    with pytest.raises(ValueError, match="whitespace"):
        f.parse_list_line("a @x.com", allow_domain=False)
    with pytest.raises(ValueError, match="person"):
        f.parse_list_line("@x.com", allow_domain=False)
    assert f.parse_list_line("x.com", allow_domain=True) == f.ParsedPattern(
        "@x.com", "domain"
    )
    assert f.parse_list_line("@x.com", allow_domain=True) == f.ParsedPattern(
        "@x.com", "domain"
    )
    items, err = f.parse_list_text(
        "A@X.com\n\na@x.com\nb@y.com\n", allow_domain=False
    )
    assert err is None
    assert [p.pattern for p in items] == ["a@x.com", "b@y.com"]
    _items, err = f.parse_list_text("ok@x.com\nbad addr\n", allow_domain=False)
    assert err is not None
    assert err.line == 2


def test_match_person_allow_beats_domain_block(tmp_path):
    db = _mk_db(tmp_path)
    roster = f.ListRoster(entries=(("example.com", "company"),))
    acc = _mk_account(
        user="u@example.com", actual_name="Rich", list_roster=roster
    )
    _seed(db, "person", "Rich", "allow", "a@x.com")
    _seed(db, "domain", "example.com", "block", "a@x.com")
    hit = f.classify_list_hit(acc, db, ["a@x.com"])
    assert hit is not None
    assert hit.decision == "allow"
    assert hit.rank == 3
    assert hit.conflict is False


def test_match_person_address_beats_domain_host_block(tmp_path):
    db = _mk_db(tmp_path)
    roster = f.ListRoster(entries=(("example.com", "company"),))
    acc = _mk_account(
        user="u@example.com", actual_name="Rich", list_roster=roster
    )
    _seed(db, "person", "Rich", "allow", "po@vendor.com")
    _seed(db, "domain", "example.com", "block", "@vendor.com", "domain")
    hit = f.classify_list_hit(acc, db, ["po@vendor.com"])
    assert hit.decision == "allow"
    assert hit.rank == 3


def test_match_domain_address_beats_domain_host(tmp_path):
    db = _mk_db(tmp_path)
    roster = f.ListRoster(entries=(("example.com", "company"),))
    acc = _mk_account(
        user="u@example.com", actual_name="Rich", list_roster=roster
    )
    _seed(db, "domain", "example.com", "block", "spam@vendor.com")
    _seed(db, "domain", "example.com", "allow", "@vendor.com", "domain")
    hit = f.classify_list_hit(acc, db, ["spam@vendor.com"])
    assert hit.decision == "block"
    assert hit.rank == 2


def test_match_same_rank_allow_wins_conflict(tmp_path):
    db = _mk_db(tmp_path)
    acc = _mk_account(actual_name="Rich")
    _seed(db, "person", "Rich", "allow", "a@x.com")
    _seed(db, "person", "Rich", "block", "a@x.com")
    hit = f.classify_list_hit(acc, db, ["a@x.com"])
    assert hit.decision == "allow"
    assert hit.conflict is True


def test_match_reply_to_only(tmp_path):
    db = _mk_db(tmp_path)
    acc = _mk_account(actual_name="Rich")
    _seed(db, "person", "Rich", "allow", "safe@x.com")
    raw = (
        b"From: other@y.com\r\n"
        b"Reply-To: Safe@X.com\r\n"
        b"Subject: hi\r\n"
        b"Message-ID: <rt@example.com>\r\n"
        b"\r\nbody\r\n"
    )
    addrs = f.iter_list_header_addrs(raw)
    assert addrs == ["other@y.com", "safe@x.com"]
    hit = f.classify_list_hit(acc, db, addrs)
    assert hit is not None
    assert hit.decision == "allow"


def test_match_ignores_domain_list_when_not_on_roster(tmp_path):
    db = _mk_db(tmp_path)
    acc = _mk_account(user="u@example.com", actual_name="Rich")
    _seed(db, "domain", "example.com", "block", "a@x.com")
    hit = f.classify_list_hit(acc, db, ["a@x.com"])
    assert hit is None


def _scan_client(raw=RAW_SCAN):
    return RecordingIMAP(
        existing=_all_existing(),
        search_uids=[1],
        fetch_by_uid={1: {b"BODY[]": raw, b"FLAGS": ()}},
    )


def test_scan_allow_skips_flag_over_threshold(tmp_path, monkeypatch):
    db = _mk_db(tmp_path)
    acc = _mk_account(mode="flag", actual_name="Rich")
    _seed(db, "person", "Rich", "allow", "sender@example.com")
    with db.tx():
        db.set_scan_bookmark("INBOX", 1, 0)
    monkeypatch.setattr(f, "rspamd_scan", lambda *a, **k: 9.0)
    client = _scan_client()
    f.scan_inbox(client, db, LOG, acc, FMAP)
    assert client.flags_added == []
    row = db.get_imap_message("INBOX", 1, 1)
    assert row["our_action"] == "allowlisted"
    assert row["our_score"] == 9.0
    evs = [r["event"] for r in db.conn.execute("SELECT event FROM events")]
    assert "allowlisted" in evs
    assert "scan" in evs


def test_scan_block_under_threshold_still_acts_flag(tmp_path, monkeypatch):
    db = _mk_db(tmp_path)
    acc = _mk_account(mode="flag", actual_name="Rich", threshold=8.0)
    _seed(db, "person", "Rich", "block", "sender@example.com")
    with db.tx():
        db.set_scan_bookmark("INBOX", 1, 0)
    monkeypatch.setattr(f, "rspamd_scan", lambda *a, **k: 2.0)
    client = _scan_client()
    f.scan_inbox(client, db, LOG, acc, FMAP)
    assert client.flags_added == [(1, [b"\\Flagged"])]
    evs = [r["event"] for r in db.conn.execute("SELECT event FROM events")]
    assert "blocklisted" in evs


def test_scan_block_shadow_does_not_move(tmp_path, monkeypatch):
    db = _mk_db(tmp_path)
    acc = _mk_account(mode="shadow", actual_name="Rich")
    _seed(db, "person", "Rich", "block", "sender@example.com")
    with db.tx():
        db.set_scan_bookmark("INBOX", 1, 0)
    monkeypatch.setattr(f, "rspamd_scan", lambda *a, **k: 9.0)
    client = _scan_client()
    f.scan_inbox(client, db, LOG, acc, FMAP)
    assert client.moved == []
    assert client.flags_added == []
    row = db.get_imap_message("INBOX", 1, 1)
    assert row["our_action"] == "shadow"


def test_list_upsert_cap(tmp_path):
    db = _mk_db(tmp_path)
    parsed = f.ParsedPattern("a@x.com", "address")
    with db.tx():
        assert db.list_upsert_address(
            "person", "Rich", "allow", parsed,
            source="imap", max_entries=1,
        ) == "inserted"
        other = f.ParsedPattern("b@x.com", "address")
        assert db.list_upsert_address(
            "person", "Rich", "allow", other,
            source="imap", max_entries=1,
        ) == "capped"
    assert db.list_count("person", "Rich", "allow") == 1
