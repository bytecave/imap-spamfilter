"""Hybrid shadow-mode write policy.

Pins the Slice 1 contract: Inbox/Junk/Trash are never mutated in
`mode=shadow`; Train-* CREATE + drain remain allowed so Bayes can be
bootstrapped during evaluation.

Run: STATE_DIR=/tmp/x python -m pytest test_shadow_mode.py
"""

import logging
import os
import sqlite3
import tempfile

os.environ.setdefault("STATE_DIR", tempfile.mkdtemp(prefix="sf_test_"))

import filter as f  # noqa: E402
from imapclient.exceptions import IMAPClientError  # noqa: E402

LOG = logging.getLogger("test")

FMAP = {
    "inbox": "INBOX",
    "junk": "Junk",
    "trash": "Trash",
    "spam_train": "Junk/Train-Spam",
    "trained_spam": "Junk/Trained-Spam",
    "ham_train": "Junk/Train-Ham",
    "trained_ham": "Junk/Trained-Ham",
}

TRAIN_FOLDERS = (
    FMAP["spam_train"],
    FMAP["trained_spam"],
    FMAP["ham_train"],
    FMAP["trained_ham"],
)

RAW_SCAN = (
    b"From: sender@example.com\r\n"
    b"To: u@example.com\r\n"
    b"Subject: suspicious offer\r\n"
    b"Message-ID: <scan1@example.com>\r\n"
    b"\r\n"
    b"hello\r\n"
)

RAW_TRAIN = (
    b"From: spammer@example.com\r\n"
    b"To: u@example.com\r\n"
    b"Subject: train me\r\n"
    b"Message-ID: <train1@example.com>\r\n"
    b"\r\n"
    b"spam body\r\n"
)


def _mk_db(tmp_path):
    db_path = tmp_path / "spamfilter.db"
    f.DB_PATH = db_path
    with sqlite3.connect(db_path) as conn:
        conn.executescript(f.SCHEMA)
    return f.Db("acct")


def _mk_account(**over):
    base = dict(
        name="acct", user="u@example.com", password="x",
        imap_host="h", imap_port=993, ssl=True,
        inbox="INBOX", junk="Junk", trash="Trash",
        spam_train="Junk/Train-Spam", trained_spam="Junk/Trained-Spam",
        ham_train="Junk/Train-Ham", trained_ham="Junk/Trained-Ham",
        mode="shadow", threshold=8.0, min_threshold_allowed=5.0,
        reject_score_above=100.0,
        move_grace_seconds=60, learn_grace_seconds=300, idle_timeout=1500,
        poll_interval=600, junk_poll_interval=120,
        retention_check_interval=3600,
        max_moves_per_hour=30, max_learns_per_hour=50, max_train_per_run=100,
        safe_mode_unseen_cap=500,
        junk_retention_days=10, trained_retention_days=7,
        learn_from_moves=True, auto_special_folders=True,
    )
    base.update(over)
    return f.Account(**base)


class RecordingIMAP:
    """IMAPClient stand-in that records mutating calls."""

    def __init__(self, existing=None, search_uids=None, fetch_by_uid=None):
        self.existing = set(existing or [])
        self.created: list[str] = []
        self.subscribed: list[str] = []
        self.moved: list[tuple[list[int], str]] = []
        self.flags_added: list[tuple] = []
        self.selects: list[tuple[str, bool]] = []
        self.search_uids = list(search_uids or [])
        self.fetch_by_uid = dict(fetch_by_uid or {})

    def select_folder(self, folder, readonly=False, **kw):
        self.selects.append((folder, bool(readonly)))
        if folder not in self.existing:
            raise IMAPClientError(f"no such folder {folder}")
        return {b"UIDVALIDITY": 1}

    def create_folder(self, name):
        self.created.append(name)
        self.existing.add(name)

    def subscribe_folder(self, name):
        self.subscribed.append(name)

    def move(self, uids, dest):
        self.moved.append((list(uids), dest))

    def add_flags(self, uid, flags):
        self.flags_added.append((uid, flags))

    def list_folders(self):
        return []

    def search(self, criteria):
        return list(self.search_uids)

    def fetch(self, uids, parts):
        out = {}
        for u in uids:
            out[u] = dict(self.fetch_by_uid.get(u, {}))
        return out


def _core_existing():
    return {"INBOX", "Junk", "Trash"}


def _all_existing():
    return _core_existing() | set(TRAIN_FOLDERS)


# ----- helpers --------------------------------------------------------------


def test_inbox_select_readonly_only_shadow():
    assert f.inbox_select_readonly(_mk_account(mode="shadow")) is True
    assert f.inbox_select_readonly(_mk_account(mode="flag")) is False
    assert f.inbox_select_readonly(_mk_account(mode="move")) is False


def test_mode_allows_retention_not_shadow():
    assert f.mode_allows_retention(_mk_account(mode="shadow")) is False
    assert f.mode_allows_retention(_mk_account(mode="flag")) is True
    assert f.mode_allows_retention(_mk_account(mode="move")) is True


# ----- ensure_folders -------------------------------------------------------


def test_ensure_folders_shadow_creates_train_not_core():
    client = RecordingIMAP(existing=_core_existing())
    f.ensure_folders(client, LOG, FMAP)
    assert client.created == list(TRAIN_FOLDERS)
    assert client.subscribed == list(TRAIN_FOLDERS)
    for name in ("INBOX", "Junk", "Trash"):
        assert name not in client.created


def test_ensure_folders_never_creates_required():
    client = RecordingIMAP(existing=set())
    try:
        f.ensure_folders(client, LOG, FMAP)
    except RuntimeError as ex:
        assert "required folder" in str(ex)
    else:
        raise AssertionError("expected RuntimeError for missing core folders")
    assert client.created == []


# ----- retention_sweep ------------------------------------------------------


def test_retention_sweep_shadow_never_moves(tmp_path, caplog):
    db = _mk_db(tmp_path)
    acc = _mk_account(mode="shadow", junk_retention_days=10)
    client = RecordingIMAP(
        existing=_all_existing(),
        search_uids=[11, 12],
        fetch_by_uid={11: {}, 12: {}},
    )
    caplog.set_level(logging.INFO)
    f.retention_sweep(client, db, LOG, acc, FMAP)
    assert client.moved == []
    assert "mode=shadow" in caplog.text
    assert "not moved to Trash" in caplog.text


def test_retention_sweep_flag_moves_to_trash(tmp_path):
    db = _mk_db(tmp_path)
    acc = _mk_account(mode="flag")
    client = RecordingIMAP(
        existing=_all_existing(),
        search_uids=[21],
        fetch_by_uid={21: {}},
    )
    f.retention_sweep(client, db, LOG, acc, FMAP)
    assert any(dest == "Trash" for _uids, dest in client.moved)


def test_retention_sweep_move_moves_to_trash(tmp_path):
    db = _mk_db(tmp_path)
    acc = _mk_account(mode="move")
    client = RecordingIMAP(
        existing=_all_existing(),
        search_uids=[22],
        fetch_by_uid={22: {}},
    )
    f.retention_sweep(client, db, LOG, acc, FMAP)
    assert any(dest == "Trash" for _uids, dest in client.moved)


# ----- scan_inbox -----------------------------------------------------------


def _scan_client():
    return RecordingIMAP(
        existing=_all_existing(),
        search_uids=[1],
        fetch_by_uid={
            1: {b"BODY[]": RAW_SCAN, b"FLAGS": ()},
        },
    )


def test_scan_inbox_shadow_logs_does_not_flag_or_enqueue(tmp_path, monkeypatch):
    db = _mk_db(tmp_path)
    acc = _mk_account(mode="shadow")
    with db.tx():
        db.set_scan_bookmark("INBOX", 1, 0)
    monkeypatch.setattr(f, "rspamd_scan", lambda *a, **k: 9.0)
    client = _scan_client()

    f.scan_inbox(client, db, LOG, acc, FMAP)

    assert client.flags_added == []
    pending = db.conn.execute("SELECT COUNT(*) FROM pending_move").fetchone()[0]
    assert pending == 0
    row = db.get_message("scan1@example.com")
    assert row is not None
    assert row["our_score"] == 9.0
    assert row["our_action"] == "shadow"
    inbox_selects = [(folder, ro) for folder, ro in client.selects if folder == "INBOX"]
    assert inbox_selects
    assert all(ro for _folder, ro in inbox_selects)


def test_scan_inbox_flag_stores_flagged(tmp_path, monkeypatch):
    db = _mk_db(tmp_path)
    acc = _mk_account(mode="flag")
    with db.tx():
        db.set_scan_bookmark("INBOX", 1, 0)
    monkeypatch.setattr(f, "rspamd_scan", lambda *a, **k: 9.0)
    client = _scan_client()

    f.scan_inbox(client, db, LOG, acc, FMAP)

    assert client.flags_added == [(1, [b"\\Flagged"])]
    inbox_selects = [(folder, ro) for folder, ro in client.selects if folder == "INBOX"]
    assert inbox_selects
    assert all(not ro for _folder, ro in inbox_selects)


def test_poll_junk_select_always_readonly(tmp_path):
    db = _mk_db(tmp_path)
    acc = _mk_account(mode="flag")
    client = RecordingIMAP(existing=_all_existing(), search_uids=[])
    f.poll_junk(client, db, LOG, acc, FMAP)
    junk_selects = [ro for folder, ro in client.selects if folder == "Junk"]
    assert junk_selects
    assert all(junk_selects)


# ----- execute_due_moves ----------------------------------------------------


def test_execute_due_moves_shadow_never_moves(tmp_path):
    db = _mk_db(tmp_path)
    acc = _mk_account(mode="shadow", move_grace_seconds=0)
    with db.tx():
        db.upsert_message("due1@example.com", "INBOX", "s@example.com", "subj")
        db.add_pending_move(1, 99, "due1@example.com")
    client = RecordingIMAP(
        existing=_all_existing(),
        fetch_by_uid={99: {b"FLAGS": ()}},
    )
    f.execute_due_moves(client, db, LOG, acc, FMAP)
    assert client.moved == []


# ----- drain Train-* (hybrid: allowed in shadow) ----------------------------


def test_drain_train_folder_shadow_still_moves(tmp_path, monkeypatch):
    db = _mk_db(tmp_path)
    acc = _mk_account(mode="shadow")
    monkeypatch.setattr(f, "rspamd_learn", lambda *a, **k: "learned")
    client = RecordingIMAP(
        existing=_all_existing(),
        search_uids=[7],
        fetch_by_uid={7: {b"BODY[]": RAW_TRAIN}},
    )
    f._drain_train_folder(
        client, db, LOG, acc, FMAP,
        kind="spam", src_key="spam_train", dst_key="trained_spam",
    )
    assert client.moved == [([7], "Junk/Trained-Spam")]
