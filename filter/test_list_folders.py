"""IMAP Allowlist/Blocklist drain (slice 11).

Run: STATE_DIR=/tmp/x python -m pytest test_list_folders.py
"""

import logging
import os
import tempfile

os.environ.setdefault("STATE_DIR", tempfile.mkdtemp(prefix="sf_test_"))

import filter as f  # noqa: E402
from test_shadow_mode import (  # noqa: E402
    FMAP,
    FILTER_OWNED_FOLDERS,
    LIST_FOLDERS,
    LOG,
    RecordingIMAP,
    _all_existing,
    _core_existing,
    _mk_account,
    _mk_db,
)

RAW_FROM = (
    b"From: a@x.com\r\n"
    b"To: u@example.com\r\n"
    b"Subject: vendor po\r\n"
    b"Message-ID: <list1@example.com>\r\n"
    b"\r\n"
    b"body\r\n"
)

RAW_EMPTY_FROM = (
    b"From: \r\n"
    b"To: u@example.com\r\n"
    b"Subject: none\r\n"
    b"Message-ID: <list2@example.com>\r\n"
    b"\r\n"
    b"body\r\n"
)


def test_ensure_creates_list_folders_not_core():
    client = RecordingIMAP(existing=_core_existing())
    f.ensure_folders(client, LOG, FMAP)
    for name in LIST_FOLDERS:
        assert name in client.created
    for name in ("INBOX", "Junk", "Trash"):
        assert name not in client.created
    assert set(client.created) == set(FILTER_OWNED_FOLDERS)


def test_drain_allowlist_inserts_person_address_and_moves(tmp_path):
    db = _mk_db(tmp_path)
    acc = _mk_account(actual_name="Rich")
    client = RecordingIMAP(
        existing=_all_existing(),
        search_uids=[1],
        fetch_by_uid={1: {b"BODY[]": RAW_FROM, b"FLAGS": ()}},
    )
    f._drain_list_folder(client, db, LOG, acc, FMAP, kind="allow")
    assert db.list_get("person", "Rich", "allow") == ["a@x.com"]
    assert client.moved == [([1], "INBOX")]
    evs = [r["event"] for r in db.conn.execute("SELECT event FROM events")]
    assert "list_imap_add" in evs


def test_drain_blocklist_flips_allow_to_block(tmp_path):
    db = _mk_db(tmp_path)
    acc = _mk_account(actual_name="Rich")
    parsed = f.ParsedPattern("a@x.com", "address")
    with db.tx():
        db.list_upsert_address(
            "person", "Rich", "allow", parsed,
            source="imap", max_entries=1000,
        )
    client = RecordingIMAP(
        existing=_all_existing(),
        search_uids=[1],
        fetch_by_uid={1: {b"BODY[]": RAW_FROM, b"FLAGS": ()}},
    )
    f._drain_list_folder(client, db, LOG, acc, FMAP, kind="block")
    assert db.list_get("person", "Rich", "allow") == []
    assert db.list_get("person", "Rich", "block") == ["a@x.com"]
    evs = [r["event"] for r in db.conn.execute("SELECT event FROM events")]
    assert "list_flip" in evs
    assert client.moved == [([1], "INBOX")]


def test_drain_empty_from_still_moves(tmp_path):
    db = _mk_db(tmp_path)
    acc = _mk_account(actual_name="Rich")
    client = RecordingIMAP(
        existing=_all_existing(),
        search_uids=[1],
        fetch_by_uid={1: {b"BODY[]": RAW_EMPTY_FROM, b"FLAGS": ()}},
    )
    f._drain_list_folder(client, db, LOG, acc, FMAP, kind="allow")
    assert db.list_get("person", "Rich", "allow") == []
    assert client.moved == [([1], "INBOX")]


def test_drain_oversize_still_moves(tmp_path):
    db = _mk_db(tmp_path)
    acc = _mk_account(actual_name="Rich")
    client = RecordingIMAP(
        existing=_all_existing(),
        search_uids=[1],
        fetch_by_uid={
            1: {b"BODY[]": RAW_FROM, b"RFC822.SIZE": f.MAX_FETCH_BYTES + 1},
        },
    )
    f._drain_list_folder(client, db, LOG, acc, FMAP, kind="allow")
    assert db.list_get("person", "Rich", "allow") == []
    assert client.moved == [([1], "INBOX")]


def test_drain_respects_max_list_per_run(tmp_path):
    db = _mk_db(tmp_path)
    acc = _mk_account(actual_name="Rich", max_list_per_run=1)
    raw2 = RAW_FROM.replace(b"a@x.com", b"b@x.com").replace(b"list1@", b"listb@")
    client = RecordingIMAP(
        existing=_all_existing(),
        search_uids=[1, 2],
        fetch_by_uid={
            1: {b"BODY[]": RAW_FROM, b"FLAGS": ()},
            2: {b"BODY[]": raw2, b"FLAGS": ()},
        },
    )
    f._drain_list_folder(client, db, LOG, acc, FMAP, kind="allow")
    assert db.list_get("person", "Rich", "allow") == ["a@x.com"]
    assert client.moved == [([1], "INBOX")]


def test_drain_cap_moves_without_insert(tmp_path):
    db = _mk_db(tmp_path)
    acc = _mk_account(actual_name="Rich", max_list_entries=1)
    with db.tx():
        db.list_upsert_address(
            "person", "Rich", "allow",
            f.ParsedPattern("old@x.com", "address"),
            source="dashboard", max_entries=1,
        )
    client = RecordingIMAP(
        existing=_all_existing(),
        search_uids=[1],
        fetch_by_uid={1: {b"BODY[]": RAW_FROM, b"FLAGS": ()}},
    )
    f._drain_list_folder(client, db, LOG, acc, FMAP, kind="allow")
    assert db.list_get("person", "Rich", "allow") == ["old@x.com"]
    evs = [r["event"] for r in db.conn.execute("SELECT event FROM events")]
    assert "list_cap" in evs
    assert client.moved == [([1], "INBOX")]
