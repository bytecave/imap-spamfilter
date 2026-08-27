"""IMAP fetch discipline: 5 MiB body cap + incremental Junk watermark.

Run: STATE_DIR=/tmp/x python -m pytest test_fetch_discipline.py
"""

import logging
import os
import tempfile

os.environ.setdefault("STATE_DIR", tempfile.mkdtemp(prefix="sf_test_"))

import filter as f  # noqa: E402
from imapclient.exceptions import IMAPClientError  # noqa: E402

from test_shadow_mode import (  # noqa: E402
    FMAP,
    RAW_SCAN,
    RAW_TRAIN,
    _all_existing,
    _mk_account,
    _mk_db,
)

LOG = logging.getLogger("test")

RAW_JUNK4 = (
    b"From: sender@example.com\r\n"
    b"To: u@example.com\r\n"
    b"Subject: new junk\r\n"
    b"Message-ID: <junk4@example.com>\r\n"
    b"\r\n"
    b"body\r\n"
)


def _wants_full_body(parts) -> bool:
    for p in parts:
        b = p if isinstance(p, bytes) else str(p).encode()
        if b in (b"BODY[]", b"BODY.PEEK[]"):
            return True
    return False


class CapIMAP:
    """Records FETCH parts and refuses BODY.PEEK[] for oversize UIDs."""

    def __init__(self, *, existing, uids, bodies, sizes=None):
        self.existing = set(existing)
        self.uids = list(uids)
        self.bodies = dict(bodies)
        self.sizes = dict(sizes or {})
        self.fetch_calls: list[tuple[list[int], list[bytes]]] = []
        self.searches: list[list] = []
        self.moved: list[tuple[list[int], str]] = []
        self.flags_added: list[tuple] = []
        self.selects: list[tuple[str, bool]] = []

    def _size(self, uid: int) -> int | None:
        if uid in self.sizes:
            return self.sizes[uid]
        if uid in self.bodies:
            return len(self.bodies[uid])
        return None

    def select_folder(self, folder, readonly=False, **kw):
        self.selects.append((folder, bool(readonly)))
        if folder not in self.existing:
            raise IMAPClientError(f"no such folder {folder}")
        return {b"UIDVALIDITY": 1}

    def move(self, uids, dest):
        self.moved.append((list(uids), dest))

    def add_flags(self, uid, flags):
        self.flags_added.append((uid, flags))

    def search(self, criteria):
        self.searches.append(list(criteria))
        if criteria and criteria[0] == "UID":
            lo = int(str(criteria[1]).split(":")[0])
            return [u for u in self.uids if u >= lo]
        if criteria and criteria[0] == "UNSEEN":
            spec = criteria[2] if len(criteria) >= 3 else "1:*"
            lo = int(str(spec).split(":")[0])
            return [u for u in self.uids if u >= lo]
        return list(self.uids)

    def fetch(self, uids, parts):
        parts_b = [p if isinstance(p, bytes) else str(p).encode() for p in parts]
        self.fetch_calls.append((list(uids), parts_b))
        if _wants_full_body(parts_b):
            for u in uids:
                size = self._size(u)
                if size is not None and size > f.MAX_FETCH_BYTES:
                    raise AssertionError(
                        f"BODY fetch for oversize uid={u} size={size}"
                    )
        out = {}
        for u in uids:
            rec = {}
            size = self._size(u)
            for p in parts_b:
                if p == b"RFC822.SIZE":
                    if size is not None:
                        rec[p] = size
                elif p in (b"BODY[]", b"BODY.PEEK[]"):
                    if u in self.bodies:
                        rec[b"BODY[]"] = self.bodies[u]
                elif p == b"FLAGS":
                    rec[p] = ()
                elif p == b"INTERNALDATE":
                    rec[p] = None
            out[u] = rec
        return out


def _body_fetch_uids(client: CapIMAP) -> list[int]:
    uids: list[int] = []
    for fetched, parts in client.fetch_calls:
        if _wants_full_body(parts):
            uids.extend(fetched)
    return uids


def _events(db):
    return [r["event"] for r in db.conn.execute("SELECT event FROM events")]


def test_scan_inbox_skips_oversize_without_body_fetch(tmp_path, monkeypatch):
    db = _mk_db(tmp_path)
    acc = _mk_account(mode="shadow")
    with db.tx():
        db.set_scan_bookmark("INBOX", 1, 0)
    monkeypatch.setattr(f, "rspamd_scan", lambda *a, **k: 9.0)
    client = CapIMAP(
        existing=_all_existing(),
        uids=[1, 2],
        bodies={1: RAW_SCAN},
        sizes={1: len(RAW_SCAN), 2: f.MAX_FETCH_BYTES + 1},
    )

    f.scan_inbox(client, db, LOG, acc, FMAP)

    assert _body_fetch_uids(client) == [1]
    assert "skipped_oversize" in _events(db)
    assert db.get_message("scan1@example.com")["our_score"] == 9.0
    assert db.get_message("scan1@example.com")["our_action"] == "shadow"
    assert client.flags_added == []
    pending = db.conn.execute("SELECT COUNT(*) FROM pending_move").fetchone()[0]
    assert pending == 0


def test_poll_junk_init_does_not_fetch_bodies(tmp_path):
    db = _mk_db(tmp_path)
    acc = _mk_account()
    client = CapIMAP(
        existing=_all_existing(),
        uids=[1, 2, 3],
        bodies={1: RAW_SCAN, 2: RAW_SCAN, 3: RAW_SCAN},
    )

    f.poll_junk(client, db, LOG, acc, FMAP)

    assert _body_fetch_uids(client) == []
    assert db.get_scan_bookmark("Junk", 1) == 3
    assert any(c[0] == "ALL" for c in client.searches)


def test_poll_junk_second_pass_fetches_only_above_bookmark(tmp_path):
    db = _mk_db(tmp_path)
    acc = _mk_account()
    with db.tx():
        db.set_scan_bookmark("Junk", 1, 3)
    client = CapIMAP(
        existing=_all_existing(),
        uids=[1, 2, 3, 4],
        bodies={4: RAW_JUNK4},
        sizes={4: len(RAW_JUNK4)},
    )

    f.poll_junk(client, db, LOG, acc, FMAP)

    assert _body_fetch_uids(client) == [4]
    size_uids = []
    for fetched, parts in client.fetch_calls:
        if b"RFC822.SIZE" in parts:
            size_uids.extend(fetched)
    assert size_uids == [4]
    assert db.get_scan_bookmark("Junk", 1) == 4


def test_drain_oversize_moves_without_learn_or_body(tmp_path, monkeypatch):
    db = _mk_db(tmp_path)
    acc = _mk_account(mode="shadow")
    learned: list = []

    def boom(*a, **k):
        learned.append(1)
        return "learned"

    monkeypatch.setattr(f, "rspamd_learn", boom)
    client = CapIMAP(
        existing=_all_existing(),
        uids=[7],
        bodies={},
        sizes={7: f.MAX_FETCH_BYTES + 10},
    )

    f._drain_train_folder(
        client, db, LOG, acc, FMAP,
        kind="spam", src_key="spam_train", dst_key="trained_spam",
    )

    assert learned == []
    assert _body_fetch_uids(client) == []
    assert client.moved == [([7], "Junk/Trained-Spam")]
    assert "skipped_oversize" in _events(db)
