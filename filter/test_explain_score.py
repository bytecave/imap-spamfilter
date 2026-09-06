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
    def __init__(self, raw: bytes):
        self.raw = raw
        self.logged_out = False
        self.selects: list[tuple[str, bool]] = []

    def list_folders(self):
        return []

    def select_folder(self, folder, readonly=False):
        self.selects.append((folder, readonly))
        return {b"EXISTS": 1}

    def fetch(self, uids, parts):
        return {uids[0]: {b"BODY[]": self.raw, b"RFC822.SIZE": len(self.raw)}}

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


def test_explain_score_requires_uid_or_msgid():
    with pytest.raises(SystemExit) as exc:
        es.main(["acct"])
    assert exc.value.code == 2
