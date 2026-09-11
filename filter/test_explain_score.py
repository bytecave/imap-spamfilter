"""Tests for explain_score.py CLI."""

import os
import tempfile
from types import SimpleNamespace

import pytest

os.environ.setdefault("STATE_DIR", tempfile.mkdtemp(prefix="sf_explain_"))

import explain_score as es  # noqa: E402
import filter as f  # noqa: E402


def _raw() -> bytes:
    return (
        b"From: auto-confirm@amazon.com\r\n"
        b"To: u@example.com\r\n"
        b"Subject: Ordered stuff\r\n"
        b"Message-ID: <amz@example.com>\r\n"
        b"\r\nbody\r\n"
    )


class FakeIMAP:
    def __init__(self, raw: bytes, *, size="auto", missing=False):
        self.raw = raw
        self.size = len(raw) if size == "auto" else size
        self.missing = missing
        self.logged_out = False
        self.selects: list[tuple[str, bool]] = []
        self.fetch_calls: list[tuple[list[int], list[bytes]]] = []

    def list_folders(self):
        return []

    def select_folder(self, folder, readonly=False):
        self.selects.append((folder, readonly))
        return {b"EXISTS": 1}

    def fetch(self, uids, parts):
        parts_b = [p if isinstance(p, bytes) else str(p).encode() for p in parts]
        self.fetch_calls.append((list(uids), parts_b))
        if self.missing:
            return {}
        rec = {}
        wants_body = any(p in (b"BODY[]", b"BODY.PEEK[]") for p in parts_b)
        if wants_body and self.size is not None and self.size > f.MAX_FETCH_BYTES:
            raise AssertionError(f"BODY fetch for oversize size={self.size}")
        for p in parts_b:
            if p == b"RFC822.SIZE":
                if self.size is not None:
                    rec[p] = self.size
            elif p in (b"BODY[]", b"BODY.PEEK[]"):
                rec[b"BODY[]"] = self.raw
            elif p == b"FLAGS":
                rec[p] = ()
            elif p == b"INTERNALDATE":
                rec[p] = None
        return {uids[0]: rec}

    def logout(self):
        self.logged_out = True


def test_explain_score_prints_symbols(monkeypatch, capsys):
    account = SimpleNamespace(
        name="acct",
        user="u@example.com",
        bayes_user="bytelord",
        reject_score_above=100.0,
        auto_special_folders=True,
        inbox="INBOX",
        junk="Junk",
        trash="Trash",
        spam_train="Junk/Train-Spam",
        trained_spam="Junk/Trained-Spam",
        ham_train="Junk/Train-Ham",
        trained_ham="Junk/Trained-Ham",
        allowlist="INBOX/Allowlist",
        blocklist="INBOX/Blocklist",
    )
    client = FakeIMAP(_raw())
    result = f.ScanResult(
        score=24.9,
        action="reject",
        symbols=(
            f.ScanSymbol("BROKEN_HEADERS", 8.0, "broken"),
            f.ScanSymbol("BLACKLIST_DMARC", 6.0, "dmarc"),
            f.ScanSymbol("BAYES_HAM", 0.0, "ham"),
        ),
    )
    monkeypatch.setattr(es, "load_accounts", lambda _p: [account])
    monkeypatch.setattr(es, "connect_imap", lambda _a: client)
    monkeypatch.setattr(es, "detect_delimiter", lambda _c: "/")
    monkeypatch.setattr(es, "apply_special_use_remap", lambda *_a, **_k: None)
    monkeypatch.setattr(
        es, "rspamd_scan_detail",
        lambda *a, **k: result,
    )
    rc = es.main(["acct", "--uid", "99"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "score=24.90" in out
    assert "BROKEN_HEADERS" in out
    assert "bayes: BAYES_HAM" in out
    assert client.selects == [("INBOX", True)]
    assert client.logged_out is True


def _account():
    return SimpleNamespace(
        name="acct",
        user="u@example.com",
        bayes_user="bytelord",
        reject_score_above=100.0,
        auto_special_folders=True,
        inbox="INBOX",
        junk="Junk",
        trash="Trash",
        spam_train="Junk/Train-Spam",
        trained_spam="Junk/Trained-Spam",
        ham_train="Junk/Train-Ham",
        trained_ham="Junk/Trained-Ham",
        allowlist="INBOX/Allowlist",
        blocklist="INBOX/Blocklist",
    )


def test_explain_score_requires_uid_or_msgid():
    with pytest.raises(SystemExit) as exc:
        es.main(["acct"])
    assert exc.value.code == 2


def test_explain_skips_oversize_without_body_fetch(monkeypatch, capsys):
    client = FakeIMAP(_raw(), size=f.MAX_FETCH_BYTES + 1)
    monkeypatch.setattr(es, "load_accounts", lambda _p: [_account()])
    monkeypatch.setattr(es, "connect_imap", lambda _a: client)
    monkeypatch.setattr(es, "detect_delimiter", lambda _c: "/")
    monkeypatch.setattr(es, "apply_special_use_remap", lambda *_a, **_k: None)
    rc = es.main(["acct", "--uid", "99"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "5 MiB" in err
    assert not any(b"BODY.PEEK[]" in parts for _uids, parts in client.fetch_calls)


def test_explain_exact_limit_fetches_body(monkeypatch, capsys):
    raw = _raw()
    client = FakeIMAP(raw, size=f.MAX_FETCH_BYTES)
    result = f.ScanResult(score=1.0, action="no action", symbols=())
    monkeypatch.setattr(es, "load_accounts", lambda _p: [_account()])
    monkeypatch.setattr(es, "connect_imap", lambda _a: client)
    monkeypatch.setattr(es, "detect_delimiter", lambda _c: "/")
    monkeypatch.setattr(es, "apply_special_use_remap", lambda *_a, **_k: None)
    monkeypatch.setattr(es, "rspamd_scan_detail", lambda *a, **k: result)
    rc = es.main(["acct", "--uid", "99"])
    assert rc == 0
    assert any(b"BODY.PEEK[]" in parts for _uids, parts in client.fetch_calls)


def test_explain_missing_size_is_fail_closed(monkeypatch, capsys):
    client = FakeIMAP(_raw(), size=None)
    monkeypatch.setattr(es, "load_accounts", lambda _p: [_account()])
    monkeypatch.setattr(es, "connect_imap", lambda _a: client)
    monkeypatch.setattr(es, "detect_delimiter", lambda _c: "/")
    monkeypatch.setattr(es, "apply_special_use_remap", lambda *_a, **_k: None)
    rc = es.main(["acct", "--uid", "7"])
    assert rc == 1
    assert not any(b"BODY.PEEK[]" in parts for _uids, parts in client.fetch_calls)


def test_explain_ambiguous_message_id_prints_candidates(tmp_path, monkeypatch, capsys):
    f.DB_PATH = tmp_path / "spamfilter.db"
    f.init_db()
    db = f.Db("acct")
    with db.tx():
        db.upsert_imap_message("INBOX", 1, 10, message_id="<dup@id>")
        db.upsert_imap_message("Junk", 2, 11, message_id="<dup@id>")
    db.close()
    monkeypatch.setattr(es, "load_accounts", lambda _p: [_account()])
    rc = es.main(["acct", "--message-id", "<dup@id>"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "ambiguous" in err
    assert "uid=10" in err
    assert "uid=11" in err
