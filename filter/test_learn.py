"""Tests for the rspamd learn path.

Covers three behaviours that together stop a permanently-unlearnable
message from looping `learn_failed` events every poll cycle:

  * rspamd_learn classifies the controller's reply (learned / already /
    declined / error) instead of collapsing it to a bool;
  * try_learn treats a deterministic decline as terminal;
  * process_pending_learns caps retries of a transient error.

Run: STATE_DIR=/tmp/x python -m pytest test_learn.py
"""

import logging
import os
import sqlite3
import tempfile
import time

os.environ.setdefault("STATE_DIR", tempfile.mkdtemp(prefix="sf_test_"))

import pytest  # noqa: E402

import filter as f  # noqa: E402

LOG = logging.getLogger("test")


# ----- helpers --------------------------------------------------------------


def _mk_db(tmp_path):
    """Fresh Db backed by a temp SQLite file with the real schema."""
    db_path = tmp_path / "spamfilter.db"
    f.DB_PATH = db_path  # Db.__init__ reads this module global
    with sqlite3.connect(db_path) as conn:
        conn.executescript(f.SCHEMA)
        f._migrate(conn)
    return f.Db("acct")


def _mk_account(**over):
    base = dict(
        name="acct", user="u@example.com", password="x",
        imap_host="h", imap_port=993, tls_mode="implicit", allow_insecure_tls=False,
        inbox="INBOX", junk="Junk", trash="Trash",
        spam_train="Junk/Train-Spam", trained_spam="Junk/Trained-Spam",
        ham_train="Junk/Train-Ham", trained_ham="Junk/Trained-Ham",
        mode="shadow", threshold=8.0, min_threshold_allowed=5.0,
        reject_score_above=100.0,
        move_grace_seconds=60, learn_grace_seconds=300, idle_timeout=1500,
        poll_interval=600, junk_poll_interval=120,
        retention_check_interval=3600,
        max_moves_per_hour=30, max_learns_per_hour=50, max_train_per_run=100,
        flip_flop_cooldown_seconds=600,
        safe_mode_unseen_cap=500,
        junk_retention_days=10, trained_retention_days=7,
        learn_from_moves=True, auto_special_folders=True,
    )
    base.update(over)
    return f.Account(**base)


def _pending(db, msgid, folder, kind, at, uid=1, uv=1):
    with db.tx():
        db.upsert_imap_message(
            folder, uv, uid,
            message_id=msgid, sender="s@example.com", subject="subj",
        )
        db.update_imap_message(
            folder, uv, uid, pending_learn=kind, pending_learn_at=at
        )


def _events(db):
    return [r["event"] for r in db.conn.execute("SELECT event FROM events")]


class _FakeResp:
    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text


class _FakeIMAP:
    """Minimal IMAPClient stand-in for process_pending_learns.

    Bodies are keyed by IMAP UID. search() is recorded so tests can
    assert we no longer look up pending learns via HEADER Message-ID.
    """

    def __init__(self, raw_by_uid, header_hits=None, uidvalidity=1):
        self._raw = dict(raw_by_uid)
        self._header_hits = header_hits
        self.uidvalidity = uidvalidity
        self.searches: list[list] = []
        self.fetches: list[list[int]] = []

    def select_folder(self, folder, **kw):
        return {b"UIDVALIDITY": self.uidvalidity}

    def search(self, criteria):
        self.searches.append(list(criteria))
        if criteria and criteria[0] == "HEADER":
            if self._header_hits is not None:
                return list(self._header_hits)
            return []
        return []

    def fetch(self, uids, parts):
        self.fetches.append(list(uids))
        parts_b = [p if isinstance(p, bytes) else str(p).encode() for p in parts]
        out = {}
        for u in uids:
            raw = self._raw.get(u)
            if raw is None:
                continue
            rec = {}
            for p in parts_b:
                if p == b"RFC822.SIZE":
                    rec[p] = len(raw)
                elif p in (b"BODY[]", b"BODY.PEEK[]"):
                    rec[b"BODY[]"] = raw
                elif p == b"FLAGS":
                    rec[p] = ()
                elif p == b"INTERNALDATE":
                    rec[p] = None
            out[u] = rec
        return out


# ----- rspamd_learn: classify the controller reply --------------------------


def test_rspamd_learn_200_is_learned(monkeypatch):
    monkeypatch.setattr(f.requests, "post",
                        lambda *a, **k: _FakeResp(200, '{"success":true}'))
    assert f.rspamd_learn(b"raw", "ham", user="u") == "learned"


def test_rspamd_learn_208_is_already(monkeypatch):
    monkeypatch.setattr(f.requests, "post",
                        lambda *a, **k: _FakeResp(208, '{"error":"already learned"}'))
    assert f.rspamd_learn(b"raw", "ham", user="u") == "already"


def test_rspamd_learn_204_is_declined(monkeypatch):
    # 204: request processed, nothing learned (too few tokens, or already
    # in that class). Retrying the identical bytes is always futile.
    monkeypatch.setattr(f.requests, "post", lambda *a, **k: _FakeResp(204, ""))
    assert f.rspamd_learn(b"raw", "ham", user="u") == "declined"


def test_rspamd_learn_5xx_is_error(monkeypatch):
    monkeypatch.setattr(f.requests, "post", lambda *a, **k: _FakeResp(503, "busy"))
    assert f.rspamd_learn(b"raw", "ham", user="u") == "error"


def test_rspamd_learn_429_is_error(monkeypatch):
    monkeypatch.setattr(f.requests, "post", lambda *a, **k: _FakeResp(429, "slow down"))
    assert f.rspamd_learn(b"raw", "ham", user="u") == "error"


@pytest.mark.parametrize("status", [401, 403])
def test_rspamd_learn_auth_is_distinct(monkeypatch, status):
    monkeypatch.setattr(f.requests, "post", lambda *a, **k: _FakeResp(status, "forbidden"))
    assert f.rspamd_learn(b"raw", "ham", user="u") == "auth"


def test_rspamd_learn_network_exception_is_error(monkeypatch):
    def boom(*a, **k):
        raise f.requests.RequestException("connection refused")

    monkeypatch.setattr(f.requests, "post", boom)
    assert f.rspamd_learn(b"raw", "ham", user="u") == "error"


def test_rspamd_learn_sends_bulk_header(monkeypatch):
    captured = {}

    def capture(url, data=None, headers=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        return _FakeResp(200, '{"success":true}')

    monkeypatch.setattr(f.requests, "post", capture)
    assert f.rspamd_learn(b"raw", "spam", user="u") == "learned"
    assert captured["headers"]["Learn-Type"] == "bulk"
    assert captured["headers"]["Password"] == f.RSPAMD_PASSWORD
    assert captured["url"].endswith("/learnspam")


# ----- try_learn: act on the classified outcome -----------------------------


def test_try_learn_declined_is_terminal(tmp_path, monkeypatch):
    db = _mk_db(tmp_path)
    acc = _mk_account()
    _pending(db, "m1", "Junk", "ham", int(time.time()))
    monkeypatch.setattr(f, "rspamd_learn", lambda *a, **k: "declined")

    ok = f.try_learn(
        db, LOG, acc, b"raw", "m1", "ham", reason="x",
        folder="Junk", uidvalidity=1, uid=1,
    )

    assert ok is True  # caller still moves the message out
    row = db.get_imap_message("Junk", 1, 1)
    assert row["pending_learn"] is None  # never retried
    assert row["learned_as"] == "unlearnable"
    evs = _events(db)
    assert "learn_skipped" in evs
    assert "learn_failed" not in evs  # dashboard banner stays clean


def test_try_learn_error_keeps_pending_for_retry(tmp_path, monkeypatch):
    db = _mk_db(tmp_path)
    acc = _mk_account()
    _pending(db, "m2", "Junk", "ham", int(time.time()))
    monkeypatch.setattr(f, "rspamd_learn", lambda *a, **k: "error")

    ok = f.try_learn(
        db, LOG, acc, b"raw", "m2", "ham", reason="x",
        folder="Junk", uidvalidity=1, uid=1,
    )

    assert ok is False
    row = db.get_imap_message("Junk", 1, 1)
    assert row["pending_learn"] == "ham"  # still queued
    assert row["learned_as"] is None
    assert "learn_failed" in _events(db)


def test_try_learn_learned_records_success(tmp_path, monkeypatch):
    db = _mk_db(tmp_path)
    acc = _mk_account()
    _pending(db, "m3", "Junk", "ham", int(time.time()))
    monkeypatch.setattr(f, "rspamd_learn", lambda *a, **k: "learned")

    ok = f.try_learn(
        db, LOG, acc, b"raw", "m3", "ham", reason="x",
        folder="Junk", uidvalidity=1, uid=1,
    )

    assert ok is True
    row = db.get_imap_message("Junk", 1, 1)
    assert row["learned_as"] == "ham"
    assert row["pending_learn"] is None
    assert "learn_ham" in _events(db)


def test_try_learn_already_records_success(tmp_path, monkeypatch):
    db = _mk_db(tmp_path)
    acc = _mk_account()
    _pending(db, "m4", "Junk", "ham", int(time.time()))
    monkeypatch.setattr(f, "rspamd_learn", lambda *a, **k: "already")

    ok = f.try_learn(
        db, LOG, acc, b"raw", "m4", "ham", reason="x",
        folder="Junk", uidvalidity=1, uid=1,
    )

    assert ok is True
    row = db.get_imap_message("Junk", 1, 1)
    assert row["learned_as"] == "ham"
    assert row["pending_learn"] is None


# ----- process_pending_learns: preserve transient failures with backoff ------


def test_process_pending_learns_backs_off_without_giving_up(tmp_path, monkeypatch):
    db = _mk_db(tmp_path)
    acc = _mk_account(learn_grace_seconds=0)
    _pending(db, "loop1", "INBOX", "ham", int(time.time()) - 10)
    monkeypatch.setattr(f, "rspamd_learn", lambda *a, **k: "error")
    client = _FakeIMAP({1: b"raw-bytes"})
    fmap = {"junk": "Junk", "inbox": "INBOX"}

    for _ in range(8):
        f.process_pending_learns(client, db, LOG, acc, fmap)

    fails = sum(e == "learn_failed" for e in _events(db))
    giveups = sum(e == "learn_giveup" for e in _events(db))
    assert fails == 1
    assert giveups == 0
    row = db.get_imap_message("INBOX", 1, 1)
    assert row["pending_learn"] == "ham"
    assert row["learned_as"] is None
    assert row["learn_retry_count"] == 1
    assert row["learn_retry_at"] > int(time.time())
    assert not any(s and s[0] == "HEADER" for s in client.searches)


def test_try_learn_flipflop_blocks_within_cooldown(tmp_path, monkeypatch):
    db = _mk_db(tmp_path)
    acc = _mk_account(flip_flop_cooldown_seconds=600)
    now = int(time.time())
    with db.tx():
        db.upsert_imap_message(
            "Junk", 1, 1,
            message_id="m-flip", sender="s@example.com", subject="subj",
        )
        db.update_imap_message(
            "Junk", 1, 1, learned_as="spam", learned_at=now,
        )
    called = {"n": 0}

    def boom(*a, **k):
        called["n"] += 1
        return "learned"

    monkeypatch.setattr(f, "rspamd_learn", boom)
    ok = f.try_learn(
        db, LOG, acc, b"raw", "m-flip", "ham", reason="x",
        folder="Junk", uidvalidity=1, uid=1,
    )
    assert ok is False
    assert called["n"] == 0
    assert "learn_flipflop_block" in _events(db)
    assert db.get_imap_message("Junk", 1, 1)["learned_as"] == "spam"


def test_try_learn_flipflop_zero_cooldown_allows_relearn(tmp_path, monkeypatch):
    db = _mk_db(tmp_path)
    acc = _mk_account(flip_flop_cooldown_seconds=0)
    now = int(time.time())
    with db.tx():
        db.upsert_imap_message(
            "Junk", 1, 1,
            message_id="m-flip0", sender="s@example.com", subject="subj",
        )
        db.update_imap_message(
            "Junk", 1, 1, learned_as="spam", learned_at=now,
        )
    monkeypatch.setattr(f, "rspamd_learn", lambda *a, **k: "learned")
    ok = f.try_learn(
        db, LOG, acc, b"raw", "m-flip0", "ham", reason="x",
        folder="Junk", uidvalidity=1, uid=1,
    )
    assert ok is True
    assert db.get_imap_message("Junk", 1, 1)["learned_as"] == "ham"
    assert "learn_ham" in _events(db)
