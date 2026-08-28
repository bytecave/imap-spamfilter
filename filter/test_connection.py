"""TLS modes, YAML booleans, and IDLE fallback.

Run: STATE_DIR=/tmp/x python -m pytest test_connection.py
"""

import logging
import os
import tempfile
from pathlib import Path

os.environ.setdefault("STATE_DIR", tempfile.mkdtemp(prefix="sf_test_"))

import pytest  # noqa: E402

import filter as f  # noqa: E402
from test_shadow_mode import _mk_account  # noqa: E402

LOG = logging.getLogger("test")


def _write_accounts(tmp_path: Path, account_lines: str, defaults: str = "") -> Path:
    body = "accounts:\n  - name: a\n    imap_host: imap.example.com\n    user: u@example.com\n    password: \"x\"\n"
    if account_lines:
        body += account_lines
    text = (f"defaults:\n{defaults}" if defaults else "") + body
    path = tmp_path / "accounts.yml"
    path.write_text(text)
    return path


def test_quoted_ssl_false_is_starttls_not_implicit(tmp_path):
    path = _write_accounts(tmp_path, '    ssl: "false"\n')
    accs = f.load_accounts(path)
    assert accs[0].tls_mode == "starttls"


def test_tls_mode_none_remote_refused(tmp_path):
    path = _write_accounts(tmp_path, "    tls_mode: none\n")
    with pytest.raises(SystemExit, match="tls_mode: none"):
        f.load_accounts(path)


def test_tls_mode_none_loopback_allowed(tmp_path):
    path = _write_accounts(
        tmp_path,
        "    tls_mode: none\n    imap_host: 127.0.0.1\n",
    )
    # overwrite host in the first account via extra keys — yaml last-key-wins
    # if we listed imap_host twice. Write a dedicated file instead.
    path.write_text(
        "accounts:\n"
        "  - name: a\n"
        "    imap_host: 127.0.0.1\n"
        "    user: u@example.com\n"
        "    password: \"x\"\n"
        "    tls_mode: none\n"
        "    imap_port: 143\n"
    )
    accs = f.load_accounts(path)
    assert accs[0].tls_mode == "none"
    assert accs[0].imap_host == "127.0.0.1"


def test_tls_mode_none_remote_with_allow_insecure(tmp_path):
    path = _write_accounts(
        tmp_path,
        "    tls_mode: none\n    allow_insecure_tls: true\n",
    )
    accs = f.load_accounts(path)
    assert accs[0].tls_mode == "none"
    assert accs[0].allow_insecure_tls is True


def test_quoted_learn_from_moves_false(tmp_path):
    path = _write_accounts(tmp_path, '    learn_from_moves: "false"\n')
    accs = f.load_accounts(path)
    assert accs[0].learn_from_moves is False


def test_invalid_bool_exits(tmp_path):
    path = _write_accounts(tmp_path, '    learn_from_moves: "maybe"\n')
    with pytest.raises(SystemExit, match="learn_from_moves"):
        f.load_accounts(path)


def test_flip_flop_cooldown_yaml_and_builtin(tmp_path):
    path = _write_accounts(tmp_path, "")
    accs = f.load_accounts(path)
    assert accs[0].flip_flop_cooldown_seconds == 600

    path = _write_accounts(tmp_path, "", defaults="  flip_flop_cooldown_seconds: 1\n")
    accs = f.load_accounts(path)
    assert accs[0].flip_flop_cooldown_seconds == 1


def test_flip_flop_cooldown_out_of_range(tmp_path):
    path = _write_accounts(tmp_path, "    flip_flop_cooldown_seconds: -1\n")
    with pytest.raises(SystemExit, match="flip_flop_cooldown_seconds"):
        f.load_accounts(path)


class _IdleProbe:
    def __init__(self):
        self.idle_calls = 0
        self.idle_done_calls = 0
        self.idle_check_calls = 0

    def select_folder(self, *a, **k):
        return {b"UIDVALIDITY": 1}

    def idle(self):
        self.idle_calls += 1

    def idle_check(self, timeout=None):
        self.idle_check_calls += 1
        return True  # activity so we don't loop

    def idle_done(self):
        self.idle_done_calls += 1


def test_wait_without_idle_does_not_call_idle(monkeypatch):
    acc = _mk_account(poll_interval=1)
    acc.folder_map = {"inbox": "INBOX"}
    client = _IdleProbe()
    monkeypatch.setattr(f.time, "sleep", lambda s: None)
    f.wait_between_scans(client, acc, idle_cap=False, log=LOG)
    assert client.idle_calls == 0
    assert client.idle_done_calls == 0


def test_wait_with_idle_calls_idle():
    acc = _mk_account()
    acc.folder_map = {"inbox": "INBOX"}
    client = _IdleProbe()
    f.wait_between_scans(client, acc, idle_cap=True, log=LOG)
    assert client.idle_calls == 1
    assert client.idle_done_calls == 1
