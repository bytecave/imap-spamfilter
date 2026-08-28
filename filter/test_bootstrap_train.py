"""Safety and outcome handling for the one-shot bootstrap training CLI."""

import os
import tempfile
from types import SimpleNamespace

import pytest

os.environ.setdefault("STATE_DIR", tempfile.mkdtemp(prefix="sf_test_"))

import bootstrap_train as bt  # noqa: E402
import filter as f  # noqa: E402


def _raw(uid: int) -> bytes:
    return (
        b"From: sender@example.com\r\n"
        b"To: user@example.com\r\n"
        + f"Subject: message {uid}\r\n".encode()
        + f"Message-ID: <{uid}@example.com>\r\n".encode()
        + b"\r\nbody\r\n"
    )


class FakeIMAP:
    def __init__(self, uids, *, bodies=None, sizes=None):
        self.uids = list(uids)
        self.bodies = dict(bodies or {uid: _raw(uid) for uid in uids})
        self.sizes = dict(
            sizes or {uid: len(raw) for uid, raw in self.bodies.items()}
        )
        self.fetch_calls: list[tuple[list[int], list[bytes]]] = []
        self.moves: list[tuple[list[int], str]] = []
        self.logged_out = False

    def select_folder(self, folder, readonly=False):
        return {b"EXISTS": len(self.uids)}

    def search(self, criteria):
        assert criteria == ["ALL"]
        return list(self.uids)

    def fetch(self, uids, parts):
        uids = list(uids)
        parts = [p if isinstance(p, bytes) else str(p).encode() for p in parts]
        self.fetch_calls.append((uids, parts))
        result = {}
        for uid in uids:
            data = {}
            if b"RFC822.SIZE" in parts and uid in self.sizes:
                data[b"RFC822.SIZE"] = self.sizes[uid]
            if b"BODY.PEEK[]" in parts and uid in self.bodies:
                data[b"BODY[]"] = self.bodies[uid]
            result[uid] = data
        return result

    def move(self, uids, destination):
        self.moves.append((list(uids), destination))

    def logout(self):
        self.logged_out = True


def _run(monkeypatch, client, outcomes, *extra_args):
    account = SimpleNamespace(
        name="acct",
        user="user@example.com",
        bayes_user=None,
    )
    outcome_iter = iter(outcomes)
    monkeypatch.setattr(bt, "RSPAMD_PASSWORD", "secret")
    monkeypatch.setattr(bt, "load_accounts", lambda _path: [account])
    monkeypatch.setattr(bt, "connect_imap", lambda _account: client)
    monkeypatch.setattr(bt, "detect_delimiter", lambda _client: "/")
    monkeypatch.setattr(
        bt,
        "rspamd_learn",
        lambda _raw, _kind, *, user: next(outcome_iter),
    )
    f.SHUTDOWN.clear()
    return bt.main(["acct", "Bootstrap-Spam", "spam", *extra_args])


def _body_fetches(client):
    return [
        uids
        for uids, parts in client.fetch_calls
        if b"BODY.PEEK[]" in parts
    ]


def _metadata_fetches(client):
    return [
        uids
        for uids, parts in client.fetch_calls
        if b"RFC822.SIZE" in parts
    ]


def test_fetches_in_chunks_and_never_fetches_oversize_body(
    monkeypatch, capsys
):
    bodies = {1: _raw(1), 3: _raw(3)}
    client = FakeIMAP(
        [1, 2, 3],
        bodies=bodies,
        sizes={1: len(bodies[1]), 2: f.MAX_FETCH_BYTES + 1, 3: len(bodies[3])},
    )
    monkeypatch.setattr(f, "SCAN_FETCH_CHUNK", 2)

    result = _run(
        monkeypatch,
        client,
        ["learned", "already"],
        "--move-to",
        "Trained-Spam",
    )

    assert result == 1
    assert _metadata_fetches(client) == [[1, 2], [3]]
    assert _body_fetches(client) == [[1], [3]]
    assert client.moves == [([1, 3], "Trained-Spam")]
    assert "learned=1 already=1 declined=0 failed=1" in capsys.readouterr().out


def test_reports_outcomes_separately_and_only_moves_successes_by_default(
    monkeypatch, capsys
):
    client = FakeIMAP([1, 2, 3, 4])

    result = _run(
        monkeypatch,
        client,
        ["learned", "already", "declined", "error"],
        "--move-to",
        "Trained-Spam",
    )

    assert result == 1
    assert client.moves == [([1, 2], "Trained-Spam")]
    assert "learned=1 already=1 declined=1 failed=1" in capsys.readouterr().out


def test_move_declined_is_explicit_and_successful(monkeypatch, capsys):
    client = FakeIMAP([1, 2, 3])

    result = _run(
        monkeypatch,
        client,
        ["learned", "already", "declined"],
        "--move-to",
        "Trained-Spam",
        "--move-declined",
    )

    assert result == 0
    assert client.moves == [([1, 2, 3], "Trained-Spam")]
    assert "learned=1 already=1 declined=1 failed=0" in capsys.readouterr().out


def test_dry_run_neither_learns_nor_moves(monkeypatch, capsys):
    client = FakeIMAP([1, 2])

    result = _run(
        monkeypatch,
        client,
        [],
        "--move-to",
        "Trained-Spam",
        "--dry-run",
    )

    assert result == 0
    assert client.moves == []
    assert "dry_run=2" in capsys.readouterr().out


@pytest.mark.parametrize("limit", ["0", "-1", "not-a-number"])
def test_limit_must_be_a_positive_integer(limit):
    with pytest.raises(SystemExit) as exc:
        bt.main(["acct", "Bootstrap-Spam", "spam", "--limit", limit])
    assert exc.value.code == 2


def test_move_declined_requires_destination():
    with pytest.raises(SystemExit) as exc:
        bt.main(["acct", "Bootstrap-Spam", "spam", "--move-declined"])
    assert exc.value.code == 2
