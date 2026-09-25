"""Rspamd /checkv2 metadata: real From, address Rcpt, no fake IP.

Run: STATE_DIR=/tmp/x python -m pytest test_rspamd_scan.py
"""

import os
import tempfile

os.environ.setdefault("STATE_DIR", tempfile.mkdtemp(prefix="sf_test_"))

import filter as f  # noqa: E402


class _FakeResp:
    def __init__(self, score=1.5, symbols=None, action="no action"):
        self._score = score
        self._symbols = symbols
        self._action = action

    def raise_for_status(self):
        return None

    def json(self):
        out = {"score": self._score, "action": self._action}
        if self._symbols is not None:
            out["symbols"] = self._symbols
        return out


def _capture_post(monkeypatch, *, score=1.5, symbols=None, action="no action"):
    captured: dict = {}

    def post(url, data=None, headers=None, timeout=None):
        captured["url"] = url
        captured["headers"] = dict(headers or {})
        captured["data"] = data
        return _FakeResp(score=score, symbols=symbols, action=action)

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
    assert captured["headers"]["Rcpt"] == "u@example.com"
    assert captured["data"].startswith(b"Delivered-To: bayes-pool\r\n")
    assert "Ip" not in captured["headers"]
    assert "Helo" not in captured["headers"]
    assert "User" not in captured["headers"]


def test_address_bayes_user_stays_in_rcpt(monkeypatch):
    captured = _capture_post(monkeypatch)
    f.rspamd_scan(
        RAW_WITH_FROM, "u@example.com", 100.0, bayes_user="pool@example.com"
    )
    assert captured["headers"]["Rcpt"] == "pool@example.com"
    # OPUS-CR-006: scan must select the same notebook learning writes to.
    # Rspamd prefers the first Delivered-To over Rcpt for the Bayes key.
    assert captured["data"].startswith(b"Delivered-To: pool@example.com\r\n")


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


def test_scan_detail_sorts_and_keeps_bayes_zero(monkeypatch):
    symbols = {
        "MIME_GOOD": {"score": -0.1, "description": "Known content-type"},
        "BROKEN_HEADERS": {"score": 8.0, "description": "Headers structure is likely broken"},
        "BAYES_HAM": {"score": 0.0, "description": "Message is probably ham"},
        "NOISE_TINY": {"score": 0.01, "description": "ignore me"},
        "BLACKLIST_DMARC": {"score": 6.0, "description": "DMARC failed"},
    }
    captured = _capture_post(
        monkeypatch, score=24.9, symbols=symbols, action="reject"
    )
    result = f.rspamd_scan_detail(
        RAW_WITH_FROM, "u@example.com", 100.0, bayes_user="bytelord"
    )
    assert result is not None
    assert result.score == 24.9
    assert result.action == "reject"
    assert captured["headers"]["Rcpt"] == "u@example.com"
    assert captured["data"].startswith(b"Delivered-To: bytelord\r\n")
    names = [s.name for s in result.symbols]
    assert names[0] == "BROKEN_HEADERS"
    assert names[1] == "BLACKLIST_DMARC"
    assert "BAYES_HAM" in names
    assert "NOISE_TINY" not in names
    assert f.rspamd_scan(RAW_WITH_FROM, "u@example.com", 100.0) == 24.9
    detail = f.score_detail_json(result)
    assert "BROKEN_HEADERS" in detail
    assert len(detail.encode()) <= f.SCORE_DETAIL_MAX_BYTES
    assert "BROKEN_HEADERS=+8.00" in f.format_top_symbols_line(result.symbols)


def _raw_with_ar(*headers: str) -> bytes:
    blob = "".join(h if h.endswith("\r\n") else h + "\r\n" for h in headers)
    blob += "From: Sender Name <sender@example.com>\r\n\r\nbody\r\n"
    return blob.encode()


_FAIL_SYMBOLS = {
    "R_DKIM_REJECT": {"score": 1.0, "description": "DKIM reject"},
    "R_SPF_FAIL": {"score": 1.0, "description": "SPF fail"},
    "DMARC_POLICY_REJECT": {"score": 2.0, "description": "DMARC reject"},
    "BLACKLIST_DMARC": {"score": 6.0, "description": "DMARC blacklist"},
    "BROKEN_HEADERS": {"score": 8.0, "description": "broken mime"},
    "HFILTER_HOSTNAME_UNKNOWN": {"score": 2.5, "description": "no hostname"},
}


def test_m365_pass_suppresses_only_matching_auth_failures(monkeypatch):
    raw = _raw_with_ar(
        "Authentication-Results: mx.microsoft.com; dkim=pass; spf=pass; dmarc=pass"
    )
    _capture_post(monkeypatch, score=20.5, symbols=_FAIL_SYMBOLS, action="reject")
    result = f.rspamd_scan_detail(raw, "u@example.com", 100.0)
    assert result is not None
    by_name = {s.name: s for s in result.symbols}
    assert by_name["R_DKIM_REJECT"].score == 0.0
    assert by_name["R_SPF_FAIL"].score == 0.0
    assert by_name["DMARC_POLICY_REJECT"].score == 0.0
    assert by_name["BLACKLIST_DMARC"].score == 0.0
    assert "mx.microsoft.com dkim=pass" in by_name["R_DKIM_REJECT"].description
    assert by_name["BROKEN_HEADERS"].score == 8.0
    assert by_name["HFILTER_HOSTNAME_UNKNOWN"].score == 2.5
    assert result.score == 20.5 - 1.0 - 1.0 - 2.0 - 6.0


def test_m365_partial_pass_keeps_failed_method(monkeypatch):
    raw = _raw_with_ar(
        "Authentication-Results: mx.microsoft.com; dkim=fail; spf=pass; dmarc=pass"
    )
    _capture_post(monkeypatch, score=10.0, symbols=_FAIL_SYMBOLS)
    result = f.rspamd_scan_detail(raw, "u@example.com", 100.0)
    assert result is not None
    by_name = {s.name: s for s in result.symbols}
    assert by_name["R_DKIM_REJECT"].score == 1.0
    assert by_name["R_SPF_FAIL"].score == 0.0
    assert by_name["DMARC_POLICY_REJECT"].score == 0.0
    assert result.score == 10.0 - 1.0 - 2.0 - 6.0


def test_untrusted_authserv_keeps_failures(monkeypatch):
    raw = _raw_with_ar(
        "Authentication-Results: evil.example; dkim=pass; spf=pass; dmarc=pass"
    )
    _capture_post(monkeypatch, score=18.5, symbols=_FAIL_SYMBOLS)
    result = f.rspamd_scan_detail(raw, "u@example.com", 100.0)
    assert result is not None
    assert result.score == 18.5
    assert {s.name: s.score for s in result.symbols}["R_DKIM_REJECT"] == 1.0


def test_outermost_m365_header_wins_over_later_copy(monkeypatch):
    raw = _raw_with_ar(
        "Authentication-Results: mx.microsoft.com; dkim=fail; spf=fail; dmarc=fail",
        "Authentication-Results: mx.microsoft.com; dkim=pass; spf=pass; dmarc=pass",
    )
    _capture_post(monkeypatch, score=10.0, symbols=_FAIL_SYMBOLS)
    result = f.rspamd_scan_detail(raw, "u@example.com", 100.0)
    assert result is not None
    assert result.score == 10.0
    assert all(s.score > 0 for s in result.symbols if s.name == "R_DKIM_REJECT")


def test_missing_ar_keeps_failures(monkeypatch):
    _capture_post(monkeypatch, score=10.0, symbols=_FAIL_SYMBOLS)
    result = f.rspamd_scan_detail(RAW_WITH_FROM, "u@example.com", 100.0)
    assert result is not None
    assert result.score == 10.0


_GOOGLE_AR = (
    "Authentication-Results: spf=pass (sender IP is 2607:f8b0:4864:20::b149) "
    "smtp.mailfrom=google.com; dkim=pass (signature was verified) "
    "header.d=google.com; dmarc=pass action=none header.from=google.com; "
    "compauth=pass reason=100"
)
_OUTLOOK_SPF = (
    "Received-SPF: Pass (protection.outlook.com: domain of google.com designates "
    "2607:f8b0:4864:20::b149 as permitted sender) receiver=protection.outlook.com; "
    "client-ip=2607:f8b0:4864:20::b149; helo=mail-yx1-xb149.google.com"
)


def test_outlook_compauth_stamp_suppresses_without_authserv_id(monkeypatch):
    raw = _raw_with_ar(_GOOGLE_AR, _OUTLOOK_SPF)
    _capture_post(monkeypatch, score=12.1, symbols=_FAIL_SYMBOLS, action="reject")
    result = f.rspamd_scan_detail(raw, "u@example.com", 100.0)
    assert result is not None
    by_name = {s.name: s for s in result.symbols}
    assert by_name["R_DKIM_REJECT"].score == 0.0
    assert by_name["DMARC_POLICY_REJECT"].score == 0.0
    assert by_name["R_SPF_FAIL"].score == 0.0
    assert "protection.outlook.com dkim=pass" in by_name["R_DKIM_REJECT"].description
    assert by_name["BROKEN_HEADERS"].score == 8.0
    assert result.score == 12.1 - 1.0 - 1.0 - 2.0 - 6.0


def test_compauth_without_outlook_received_spf_keeps_failures(monkeypatch):
    raw = _raw_with_ar(_GOOGLE_AR)
    _capture_post(monkeypatch, score=12.1, symbols=_FAIL_SYMBOLS)
    result = f.rspamd_scan_detail(raw, "u@example.com", 100.0)
    assert result is not None
    assert result.score == 12.1


def test_compauth_with_other_spf_receiver_keeps_failures(monkeypatch):
    raw = _raw_with_ar(
        _GOOGLE_AR,
        "Received-SPF: Pass (google.com) receiver=mail.google.com; client-ip=1.2.3.4",
    )
    _capture_post(monkeypatch, score=12.1, symbols=_FAIL_SYMBOLS)
    result = f.rspamd_scan_detail(raw, "u@example.com", 100.0)
    assert result is not None
    assert result.score == 12.1


def test_later_compauth_header_does_not_override_outermost(monkeypatch):
    raw = _raw_with_ar(
        "Authentication-Results: evil.example; dkim=fail; spf=fail; dmarc=fail",
        _GOOGLE_AR,
        _OUTLOOK_SPF,
    )
    _capture_post(monkeypatch, score=12.1, symbols=_FAIL_SYMBOLS)
    result = f.rspamd_scan_detail(raw, "u@example.com", 100.0)
    assert result is not None
    assert result.score == 12.1


def test_later_mx_microsoft_header_does_not_override_outermost(monkeypatch):
    raw = _raw_with_ar(
        "Authentication-Results: evil.example; dkim=fail; spf=fail; dmarc=fail",
        "Authentication-Results: mx.microsoft.com; dkim=pass; spf=pass; dmarc=pass",
    )
    _capture_post(monkeypatch, score=10.0, symbols=_FAIL_SYMBOLS)
    result = f.rspamd_scan_detail(raw, "u@example.com", 100.0)
    assert result is not None
    assert result.score == 10.0


def test_mixed_dkim_pass_and_fail_is_not_a_clean_pass(monkeypatch):
    raw = _raw_with_ar(
        "Authentication-Results: mx.microsoft.com; dkim=pass header.d=a.com; dkim=fail header.d=b.com; spf=softfail"
    )
    _capture_post(monkeypatch, score=2.0, symbols=_FAIL_SYMBOLS)
    result = f.rspamd_scan_detail(raw, "u@example.com", 100.0)
    assert result is not None
    by_name = {s.name: s for s in result.symbols}
    assert by_name["R_DKIM_REJECT"].score == 1.0
    assert by_name["R_SPF_FAIL"].score == 1.0


def test_parse_envelope_decodes_rfc2047_subject():
    raw = (
        b"From: a@example.com\r\n"
        b"Message-ID: <x@example.com>\r\n"
        b"Subject: =?utf-8?Q?Hello_World?=\r\n"
        b"\r\nbody\r\n"
    )
    msgid, subject, sender = f.parse_envelope(raw)
    assert msgid == "x@example.com"
    assert subject == "Hello World"
    assert sender == "a@example.com"


def test_scan_identity_precedes_message_delivered_to(monkeypatch):
    """A message that already carries Delivered-To (Postfix/Dovecot/Gmail)
    must still be classified under the configured identity (OPUS-CR-006)."""
    captured = _capture_post(monkeypatch)
    raw = b"Delivered-To: someone-else@example.net\r\n" + RAW_WITH_FROM
    f.rspamd_scan(raw, "u@example.com", 100.0, bayes_user="u@example.com")
    first = captured["data"].split(b"\r\n", 1)[0]
    assert first == b"Delivered-To: u@example.com"
    assert captured["headers"]["Rcpt"] == "u@example.com"


def test_scan_without_bayes_user_prefixes_recipient_identity(monkeypatch):
    captured = _capture_post(monkeypatch)
    f.rspamd_scan(RAW_WITH_FROM, "u@example.com", 100.0)
    assert captured["data"].startswith(b"Delivered-To: u@example.com\r\n")
