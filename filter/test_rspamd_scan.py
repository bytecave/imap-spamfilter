"""Rspamd /checkv2 metadata: real From, Rcpt as Bayes identity, no fake IP.

Run: STATE_DIR=/tmp/x python -m pytest test_rspamd_scan.py
"""

import os
import tempfile

os.environ.setdefault("STATE_DIR", tempfile.mkdtemp(prefix="sf_test_"))

import filter as f  # noqa: E402


class _FakeResp:
    def __init__(self, score=1.5):
        self._score = score

    def raise_for_status(self):
        return None

    def json(self):
        return {"score": self._score}


def _capture_post(monkeypatch):
    captured: dict = {}

    def post(url, data=None, headers=None, timeout=None):
        captured["url"] = url
        captured["headers"] = dict(headers or {})
        captured["data"] = data
        return _FakeResp()

    monkeypatch.setattr(f.requests, "post", post)
    return captured


RAW_WITH_FROM = (
    b"From: Sender Name <sender@example.com>\r\n"
    b"To: u@example.com\r\n"
    b"Subject: hi\r\n"
    b"\r\n"
    b"body\r\n"
)

RAW_NO_FROM = (
    b"To: u@example.com\r\n"
    b"Subject: hi\r\n"
    b"\r\n"
    b"body\r\n"
)


def test_scan_from_is_message_from_not_recipient(monkeypatch):
    captured = _capture_post(monkeypatch)
    score = f.rspamd_scan(
        RAW_WITH_FROM, "u@example.com", 100.0, bayes_user="bayes-pool"
    )
    assert score == 1.5
    assert captured["headers"]["From"] == "sender@example.com"
    assert captured["headers"]["Rcpt"] == "bayes-pool"
    assert "Ip" not in captured["headers"]
    assert "Helo" not in captured["headers"]
    assert "User" not in captured["headers"]


def test_scan_omits_from_when_missing(monkeypatch):
    captured = _capture_post(monkeypatch)
    score = f.rspamd_scan(RAW_NO_FROM, "u@example.com", 100.0)
    assert score == 1.5
    assert "From" not in captured["headers"]
    assert captured["headers"]["Rcpt"] == "u@example.com"


def test_scan_rcpt_falls_back_to_recipient(monkeypatch):
    captured = _capture_post(monkeypatch)
    f.rspamd_scan(RAW_WITH_FROM, "u@example.com", 100.0)
    assert captured["headers"]["Rcpt"] == "u@example.com"
    assert captured["headers"]["From"] == "sender@example.com"
