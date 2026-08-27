"""Secrets file loading (ByteLord VPS path).

Run: STATE_DIR=/tmp/x python -m pytest test_secrets.py
"""

import os
import tempfile
from pathlib import Path

os.environ.setdefault("STATE_DIR", tempfile.mkdtemp(prefix="sf_test_"))

import filter as f  # noqa: E402


def test_parse_env_file_ignores_comments_and_quotes(tmp_path, monkeypatch):
    path = tmp_path / "secrets.env"
    path.write_text(
        "# comment\n"
        'RSPAMD_PASSWORD="from-file"\n'
        "REDIS_PASSWORD=redis-secret\n"
    )
    monkeypatch.delenv("RSPAMD_PASSWORD", raising=False)
    monkeypatch.setenv("SECRETS_FILE", str(path))
    assert f._load_secret("RSPAMD_PASSWORD") == "from-file"
    assert f._load_secret("REDIS_PASSWORD") == "redis-secret"


def test_env_wins_over_secrets_file(tmp_path, monkeypatch):
    path = tmp_path / "secrets.env"
    path.write_text("RSPAMD_PASSWORD=from-file\n")
    monkeypatch.setenv("SECRETS_FILE", str(path))
    monkeypatch.setenv("RSPAMD_PASSWORD", "from-env")
    assert f._load_secret("RSPAMD_PASSWORD") == "from-env"


def test_load_rspamd_password_reads_secrets_file(tmp_path, monkeypatch):
    path = tmp_path / "secrets.env"
    path.write_text("RSPAMD_PASSWORD=controller-secret\n")
    monkeypatch.delenv("RSPAMD_PASSWORD", raising=False)
    monkeypatch.setenv("SECRETS_FILE", str(path))
    assert f._load_rspamd_password() == "controller-secret"


def test_load_rspamd_password_falls_back_to_controller_file(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    (state / "controller.password").write_text("appdata-secret\n")
    monkeypatch.delenv("RSPAMD_PASSWORD", raising=False)
    monkeypatch.setenv("SECRETS_FILE", str(tmp_path / "missing.env"))
    monkeypatch.setenv("STATE_DIR", str(state))
    assert f._load_rspamd_password() == "appdata-secret"
