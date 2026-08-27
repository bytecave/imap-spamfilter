"""IMAP/exception log redaction.

Run: STATE_DIR=/tmp/x python -m pytest test_log_redact.py
"""

import os
import tempfile

os.environ.setdefault("STATE_DIR", tempfile.mkdtemp(prefix="sf_test_"))

import filter as f  # noqa: E402


def test_redact_log_replaces_secret():
    out = f.redact_log("LOGIN user secretpass ok", "secretpass")
    assert "secretpass" not in out
    assert "***" in out


def test_redact_log_strips_login_payload():
    out = f.redact_log('NO LOGIN "u" "pw" failed')
    assert out.startswith("NO LOGIN ***")
    assert '"pw"' not in out
