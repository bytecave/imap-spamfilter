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

    def list_folders(self):
        return []

    def select_folder(self, folder, readonly=False):
        return {b"EXISTS": len(self.uids), b"UIDVALIDITY": 1}

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


def _trained_account(name: str) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        user=f"{name}@example.com",
        bayes_user="bytelord",
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


class FolderIMAP(FakeIMAP):
    """IMAP stub that switches UID sets when selecting Trained-* folders."""

    def __init__(self, folders: dict[str, list[int]], *, missing=()):
        super().__init__([])
        self.folder_uids = {k: list(v) for k, v in folders.items()}
        self.missing = set(missing)
        self.selects: list[tuple[str, bool]] = []
        self.current = None

    def select_folder(self, folder, readonly=False):
        self.selects.append((folder, readonly))
        if folder in self.missing or folder not in self.folder_uids:
            from imapclient.exceptions import IMAPClientError
            raise IMAPClientError(f"NO {folder}")
        self.current = folder
        self.uids = list(self.folder_uids[folder])
        self.bodies = {uid: _raw(uid) for uid in self.uids}
        self.sizes = {uid: len(self.bodies[uid]) for uid in self.uids}
        return {b"EXISTS": len(self.uids), b"UIDVALIDITY": 1}


def _run_all(monkeypatch, clients, outcomes, *extra_args):
    accounts = [_trained_account("one"), _trained_account("two")]
    by_name = dict(clients)
    outcome_iter = iter(outcomes)
    learned_users: list[str] = []

    def fake_learn(_raw, kind, *, user):
        learned_users.append(user)
        return next(outcome_iter)

    monkeypatch.setattr(bt, "RSPAMD_PASSWORD", "secret")
    monkeypatch.setattr(bt, "load_accounts", lambda _path: accounts)
    monkeypatch.setattr(bt, "connect_imap", lambda acc: by_name[acc.name])
    monkeypatch.setattr(bt, "detect_delimiter", lambda _client: "/")
    monkeypatch.setattr(bt, "rspamd_learn", fake_learn)
    f.SHUTDOWN.clear()
    rc = bt.main(["--all-trained", *extra_args])
    return rc, learned_users


def test_all_trained_walks_both_kinds_and_does_not_move(monkeypatch, capsys):
    one = FolderIMAP({
        "Junk/Trained-Spam": [1, 2],
        "Junk/Trained-Ham": [3],
    })
    two = FolderIMAP({
        "Junk/Trained-Spam": [10],
        "Junk/Trained-Ham": [11],
    })
    rc, users = _run_all(
        monkeypatch,
        {"one": one, "two": two},
        ["learned", "already", "learned", "already", "learned"],
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert one.moves == []
    assert two.moves == []
    assert all(readonly for _folder, readonly in one.selects)
    assert all(readonly for _folder, readonly in two.selects)
    assert users == ["bytelord"] * 5
    assert "learned=3 already=2 declined=0 failed=0" in out
    assert "skipped_folders=0" in out
    assert "[one]" in out and "[two]" in out


def test_all_trained_skips_missing_folder(monkeypatch, capsys):
    one = FolderIMAP(
        {"Junk/Trained-Spam": [1]},
        missing={"Junk/Trained-Ham"},
    )
    two = FolderIMAP({
        "Junk/Trained-Spam": [],
        "Junk/Trained-Ham": [2],
    })
    rc, users = _run_all(
        monkeypatch,
        {"one": one, "two": two},
        ["learned", "already"],
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "skip Junk/Trained-Ham" in out
    assert "skipped_folders=1" in out
    assert users == ["bytelord", "bytelord"]
    assert "learned=1 already=1" in out
    assert one.moves == []
    assert two.moves == []


def test_all_trained_rejects_move_to():
    with pytest.raises(SystemExit) as exc:
        bt.main(["--all-trained", "--move-to", "Trained-Spam"])
    assert exc.value.code == 2


def test_all_trained_kind_spam_only(monkeypatch, capsys):
    one = FolderIMAP({
        "Junk/Trained-Spam": [1],
        "Junk/Trained-Ham": [2],
    })
    two = FolderIMAP({
        "Junk/Trained-Spam": [3],
        "Junk/Trained-Ham": [4],
    })
    rc, users = _run_all(
        monkeypatch,
        {"one": one, "two": two},
        ["learned", "already"],
        "--kind",
        "spam",
    )
    assert rc == 0
    assert users == ["bytelord", "bytelord"]
    selected = [folder for folder, _ro in one.selects + two.selects]
    assert selected == ["Junk/Trained-Spam", "Junk/Trained-Spam"]


def _mk_learn_db(tmp_path, account="acct"):
    f.DB_PATH = tmp_path / "spamfilter.db"
    f.init_db()
    return f.Db(account)


def test_train_folder_writes_messages_and_learn_events(tmp_path, monkeypatch):
    db = _mk_learn_db(tmp_path)
    acc = SimpleNamespace(name="acct", user="user@example.com", bayes_user="bytelord")
    client = FakeIMAP([1, 2, 3])
    outcomes = iter(["learned", "already", "declined"])
    monkeypatch.setattr(
        bt, "rspamd_learn", lambda _raw, _kind, *, user: next(outcomes)
    )
    f.SHUTDOWN.clear()
    counts, skipped = bt.train_folder(
        client, acc,
        src="Junk/Trained-Spam", kind="spam",
        dry_run=False, limit=10_000, db=db,
    )
    assert skipped is False
    assert counts["learned"] == 1
    assert counts["already"] == 1
    assert counts["declined"] == 1
    row1 = db.get_imap_message("Junk/Trained-Spam", 1, 1)
    row2 = db.get_imap_message("Junk/Trained-Spam", 1, 2)
    row3 = db.get_imap_message("Junk/Trained-Spam", 1, 3)
    assert row1["learned_as"] == "spam"
    assert row1["sender"] == "sender@example.com"
    assert row1["message_id"] == "1@example.com"
    assert row2["learned_as"] == "spam"
    assert row3 is None
    evs = list(db.conn.execute(
        "SELECT event, message_id, detail FROM events ORDER BY rowid"
    ))
    assert [r["event"] for r in evs] == ["learn_spam", "learn_spam"]
    assert all("bootstrap" in (r["detail"] or "") for r in evs)
    db.close()


def test_train_folder_dry_run_does_not_write_db(tmp_path, monkeypatch):
    db = _mk_learn_db(tmp_path)
    acc = SimpleNamespace(name="acct", user="user@example.com", bayes_user=None)
    client = FakeIMAP([1])
    monkeypatch.setattr(
        bt, "rspamd_learn", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("no learn"))
    )
    f.SHUTDOWN.clear()
    counts, _skipped = bt.train_folder(
        client, acc,
        src="Junk/Trained-Ham", kind="ham",
        dry_run=True, limit=10_000, db=db,
    )
    assert counts["dry_run"] == 1
    assert db.get_imap_message("Junk/Trained-Ham", 1, 1) is None
    evs = list(db.conn.execute("SELECT event FROM events"))
    assert evs == []
    db.close()


def test_train_folder_skips_allowlisted_spam(tmp_path, monkeypatch):
    db = _mk_learn_db(tmp_path)
    acc = SimpleNamespace(
        name="acct",
        user="user@example.com",
        bayes_user="bytelord",
        actual_name="Rich",
        list_roster=f.ListRoster(),
    )
    parsed = f.parse_list_line("sender@example.com", allow_domain=False)
    with db.tx():
        assert db.list_upsert_address(
            "person", "Rich", "allow", parsed,
            source="dashboard", actor="test", max_entries=1000,
        ) == "inserted"
    client = FakeIMAP([1])
    monkeypatch.setattr(
        bt, "rspamd_learn",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("no learn")),
    )
    f.SHUTDOWN.clear()
    counts, skipped_folder = bt.train_folder(
        client, acc,
        src="Junk/Trained-Spam", kind="spam",
        dry_run=False, limit=10_000, db=db,
    )
    assert skipped_folder is False
    assert counts["skipped"] == 1
    assert counts["learned"] == 0
    assert counts["failed"] == 0
    row = db.get_imap_message("Junk/Trained-Spam", 1, 1)
    assert row is not None
    assert row["learned_as"] is None
    evs = [r["event"] for r in db.conn.execute("SELECT event FROM events")]
    assert evs == ["learn_skipped_list"]
    db.close()
