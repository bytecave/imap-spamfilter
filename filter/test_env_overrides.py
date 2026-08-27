"""YAML defaults: win over Unraid retention env vars.

Run: STATE_DIR=/tmp/x python -m pytest test_env_overrides.py
"""

import os
import tempfile
from pathlib import Path

os.environ.setdefault("STATE_DIR", tempfile.mkdtemp(prefix="sf_test_"))

import filter as f  # noqa: E402
from test_connection import _write_accounts  # noqa: E402


def test_yaml_junk_retention_wins_over_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DEFAULT_JUNK_RETENTION_DAYS", "10")
    path = _write_accounts(tmp_path, "", defaults="  junk_retention_days: 30\n")
    accs = f.load_accounts(path)
    assert accs[0].junk_retention_days == 30


def test_env_fills_when_yaml_omits_key(tmp_path, monkeypatch):
    monkeypatch.setenv("DEFAULT_JUNK_RETENTION_DAYS", "10")
    monkeypatch.setenv("DEFAULT_TRAINED_RETENTION_DAYS", "3")
    path = _write_accounts(tmp_path, "", defaults="  threshold: 9.0\n")
    accs = f.load_accounts(path)
    assert accs[0].junk_retention_days == 10
    assert accs[0].trained_retention_days == 3
    assert accs[0].threshold == 9.0


def test_builtin_when_env_and_yaml_omit(tmp_path, monkeypatch):
    monkeypatch.delenv("DEFAULT_JUNK_RETENTION_DAYS", raising=False)
    monkeypatch.delenv("DEFAULT_TRAINED_RETENTION_DAYS", raising=False)
    path = _write_accounts(tmp_path, "")
    accs = f.load_accounts(path)
    assert accs[0].junk_retention_days == 10
    assert accs[0].trained_retention_days == 7
