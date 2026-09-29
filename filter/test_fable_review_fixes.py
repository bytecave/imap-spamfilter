"""Regression tests for the Claude Fable 5.1 review fixes (FABLE-CR-*).

See CLAUDE_FABLE5.1_CODE_REVIEW.md for each finding and
CLAUDE_FABLE5.1_CODE_FIXED.md for what changed.
"""

import logging
import os
import socket
import tempfile
import threading

os.environ.setdefault("STATE_DIR", tempfile.mkdtemp(prefix="sf_test_"))

import pytest  # noqa: E402
from imapclient import IMAPClient  # noqa: E402

import filter as f  # noqa: E402
from test_shadow_mode import (  # noqa: E402
    FMAP, RecordingIMAP, _all_existing, _mk_account, _mk_db,
)

LOG = logging.getLogger("test")


class _SearchLog(RecordingIMAP):
    """RecordingIMAP that also records every SEARCH criteria list."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.searches: list[list] = []

    def search(self, criteria):
        crit = list(criteria) if isinstance(criteria, (list, tuple)) else [criteria]
        self.searches.append(crit)
        return super().search(criteria)

    def header_searches(self):
        return [c for c in self.searches if any(str(t).upper() == "HEADER" for t in c)]


# ----- FABLE-CR-002: Message-ID must not reach IMAP SEARCH unsafely ---------


@pytest.mark.parametrize("msgid", [
    "abc{5}",            # IMAP synchronizing-literal marker at end of line
    "a\r\n b@x",         # folded header: CR/LF would split the command
    'a"b@x',             # quote
    "a\\b@x",            # backslash
    "j��rg@x",  # 8-bit bytes as compat32 renders them
    "a b@x",             # whitespace
    "(a)@x",             # parenthesis
    "",
])
def test_unsafe_message_ids_are_not_searchable(msgid):
    assert not f._search_safe_msgid(msgid)


@pytest.mark.parametrize("msgid", [
    "CAF=abc+123@mail.gmail.com",
    "0100018f.abc-def@email.amazonses.com",
    "BN8PR12MB1234.namprd12.prod.outlook.com",
    "a#b$c%d&e'f*g/h=i?j^k_l`m|n~o@x",
])
def test_ordinary_message_ids_stay_searchable(msgid):
    assert f._search_safe_msgid(msgid)


def test_unsafe_message_id_sends_no_header_search(tmp_path):
    client = _SearchLog(existing=_all_existing())
    found = f._identical_copies_in_folder(
        client, FMAP["inbox"], {("abc{5}", "deadbeef")}, LOG, password="x",
    )
    assert found == set()
    assert client.header_searches() == []


def test_mixed_wanted_set_still_searches_the_safe_id(tmp_path):
    client = _SearchLog(existing=_all_existing())
    f._identical_copies_in_folder(
        client, FMAP["inbox"],
        {("abc{5}", "aa"), ("ok@example.com", "bb")}, LOG, password="x",
    )
    needles = [str(c[-1]) for c in client.header_searches()]
    assert needles and all("ok@example.com" in n for n in needles)


def test_train_leftover_with_literal_message_id_is_kept_not_fatal(tmp_path):
    """The Exchange MOVE-as-COPY leftover path runs the SEARCH on every pass."""
    db = _mk_db(tmp_path)
    acc = _mk_account()
    with db.tx():
        db.upsert_imap_message(
            FMAP["spam_train"], 1, 7,
            message_id="spam{5}", body_sha256="ab" * 32,
        )
        db.update_imap_message(
            FMAP["spam_train"], 1, 7, current_folder=FMAP["trained_spam"],
        )
    client = _SearchLog(existing=_all_existing(), search_uids=[7])
    f.drain_train_spam(client, db, LOG, acc, FMAP)
    assert client.header_searches() == []
    assert client.expunged == []
    assert client.moved == []


def _literal_honouring_server(sock: socket.socket) -> None:
    """Minimal IMAP server that treats a trailing {n} as a real literal."""
    conn, _ = sock.accept()
    fh = conn.makefile("rb")
    conn.sendall(b"* OK fake ready\r\n")
    while True:
        line = fh.readline()
        if not line:
            return
        tag = line.split(b" ", 1)[0]
        up = line.upper()
        stripped = line.rstrip()
        if stripped.endswith(b"}") and b"{" in stripped:
            size = int(stripped[stripped.rindex(b"{") + 1:-1])
            conn.sendall(b"+ Ready for literal\r\n")
            fh.read(size)
            fh.readline()
            conn.sendall(tag + b" OK SEARCH done\r\n")
        elif b"CAPABILITY" in up:
            conn.sendall(b"* CAPABILITY IMAP4rev1\r\n" + tag + b" OK done\r\n")
        elif b"LOGIN" in up:
            conn.sendall(tag + b" OK logged in\r\n")
        elif b"SELECT" in up or b"EXAMINE" in up:
            conn.sendall(
                b"* 0 EXISTS\r\n* OK [UIDVALIDITY 1] x\r\n"
                + tag + b" OK [READ-ONLY] done\r\n"
            )
        elif b"SEARCH" in up:
            conn.sendall(b"* SEARCH\r\n" + tag + b" OK SEARCH done\r\n")
        elif b"LOGOUT" in up:
            conn.sendall(b"* BYE\r\n" + tag + b" OK bye\r\n")
            return
        else:
            conn.sendall(tag + b" OK done\r\n")


def test_literal_marker_message_id_does_not_hang_a_real_imap_session():
    """Before the fix this raised TimeoutError (an OSError, not an
    IMAPClientError), which aborted the account pass on every retry."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    threading.Thread(target=_literal_honouring_server, args=(srv,), daemon=True).start()
    client = IMAPClient("127.0.0.1", port=srv.getsockname()[1], ssl=False, timeout=3)
    try:
        client.login("u", "p")
        found = f._identical_copies_in_folder(
            client, "Junk", {("abc{5}", "deadbeef")}, LOG, password="p",
        )
        assert found == set()
        # The session is still usable afterwards.
        assert client.search(["ALL"]) == []
    finally:
        srv.close()


# ----- FABLE-CR-003: 8-bit addresses must not break the rspamd POST ---------

RAW_8BIT_TO = (
    b"From: a@b.com\r\nTo: <r\xc3\xafch@example.com>\r\n"
    b"Subject: x\r\nMessage-ID: <m1@b.com>\r\n\r\nbody\r\n"
)
RAW_8BIT_FROM = (
    b"From: <j\xc3\xb6rg@example.de>\r\nTo: u@example.com\r\n"
    b"Subject: x\r\nMessage-ID: <m2@b.com>\r\n\r\nbody\r\n"
)


def test_first_recipient_skips_an_address_http_cannot_carry():
    assert f.first_recipient(RAW_8BIT_TO, "u@example.com") == "u@example.com"
    raw = b"From: a@b.com\r\nTo: <\xc3\xa9@x.com>\r\nCc: ok@x.com\r\n\r\nb\r\n"
    assert f.first_recipient(raw, "u@example.com") == "ok@x.com"
    assert f.first_recipient(b"To: plain@x.com\r\n\r\nb\r\n", "u@x") == "plain@x.com"


def test_scan_request_headers_are_always_printable_ascii(monkeypatch):
    captured: dict[str, str] = {}

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"score": 1.0, "action": "no action", "symbols": {}}

    def fake_post(url, data=None, headers=None, timeout=None):
        captured.clear()
        captured.update(headers or {})
        return _Resp()

    monkeypatch.setattr(f.requests, "post", fake_post)
    rcpt = f.first_recipient(RAW_8BIT_TO, "u@example.com")
    assert f.rspamd_scan_detail(RAW_8BIT_TO, rcpt, 100.0, bayes_user="bytelord")
    assert captured["Rcpt"] == "u@example.com"
    assert captured["From"] == "a@b.com"
    rcpt = f.first_recipient(RAW_8BIT_FROM, "u@example.com")
    assert f.rspamd_scan_detail(RAW_8BIT_FROM, rcpt, 100.0, bayes_user="bytelord")
    assert "From" not in captured
    assert all(v.isascii() and v.isprintable() for v in captured.values())


def test_8bit_addresses_scan_over_a_real_http_connection(monkeypatch):
    """http.client encodes header values as Latin-1; before the fix this
    raised UnicodeEncodeError and rspamd_scan_detail returned None on every
    attempt, so the message halted the scan until the poison give-up."""
    import http.server

    class _H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            body = b'{"score": 2.5, "action": "no action", "symbols": {}}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        monkeypatch.setattr(
            f, "RSPAMD_SCAN_URL",
            f"http://127.0.0.1:{srv.server_address[1]}/checkv2",
        )
        for raw in (RAW_8BIT_TO, RAW_8BIT_FROM):
            rcpt = f.first_recipient(raw, "u@example.com")
            result = f.rspamd_scan_detail(raw, rcpt, 100.0, bayes_user="bytelord")
            assert result is not None and result.score == 2.5
    finally:
        srv.shutdown()


# ----- FABLE-CR-009: a Train-Ham copy of Junk mail is not a user revert -----

RAW_RESTORE = (
    b"From: sender@example.com\r\n"
    b"To: u@example.com\r\n"
    b"Subject: mis-junked invoice\r\n"
    b"Message-ID: <restore1@example.com>\r\n"
    b"\r\n"
    b"hello\r\n"
)


def _seed_junk_origin_restore(db, *, block=False):
    sha = f.body_sha256(RAW_RESTORE)
    with db.tx():
        db.set_scan_bookmark("INBOX", 1, 0)
        # poll_junk saw the message in Junk; the user then dragged it to
        # Train-Ham, whose drain copied it to the Inbox.
        db.upsert_imap_message(
            FMAP["junk"], 1, 55, message_id="restore1@example.com", body_sha256=sha,
        )
        db.upsert_imap_message(
            FMAP["ham_train"], 1, 7, message_id="restore1@example.com", body_sha256=sha,
        )
        db.update_imap_message(FMAP["ham_train"], 1, 7, our_action="inbox_copied")
        if block:
            db.list_upsert_address(
                "person", "Test User", "block",
                f.ParsedPattern("sender@example.com", "address"),
                source="imap", max_entries=1000,
            )


def _scan_one(db, acc, monkeypatch, uid=1, score=9.0):
    monkeypatch.setattr(
        f, "rspamd_scan_detail", lambda *a, **k: f.ScanResult(score, (), None),
    )
    client = RecordingIMAP(
        existing=_all_existing(),
        search_uids=[uid],
        fetch_by_uid={uid: {b"BODY[]": RAW_RESTORE, b"FLAGS": ()}},
    )
    f.scan_inbox(client, db, LOG, acc, FMAP)
    return db.get_imap_message("INBOX", 1, uid)


def _pending_moves(db):
    return db.conn.execute("SELECT COUNT(*) FROM pending_move").fetchone()[0]


def test_junk_origin_restore_is_scored_and_held(tmp_path, monkeypatch):
    db = _mk_db(tmp_path)
    acc = _mk_account(mode="move", move_grace_seconds=0, actual_name="Test User")
    _seed_junk_origin_restore(db)
    row = _scan_one(db, acc, monkeypatch)
    assert row["our_score"] == 9.0
    assert row["our_action"] == "ham_restored"
    assert row["pending_learn"] is None
    assert _pending_moves(db) == 0


def test_junk_origin_restore_blocklist_still_junks(tmp_path, monkeypatch):
    db = _mk_db(tmp_path)
    acc = _mk_account(mode="move", move_grace_seconds=0, actual_name="Test User")
    _seed_junk_origin_restore(db, block=True)
    row = _scan_one(db, acc, monkeypatch)
    assert row["our_action"] == "pending_move"
    assert row["pending_learn"] is None
    assert _pending_moves(db) == 1


def test_later_arrival_of_a_restored_body_is_a_user_revert(tmp_path, monkeypatch):
    """Once the restore copy reached the Inbox, the same bytes coming back
    from Junk are the user's drag and must still schedule the ham learn."""
    db = _mk_db(tmp_path)
    acc = _mk_account(mode="move", move_grace_seconds=0, actual_name="Test User")
    _seed_junk_origin_restore(db)
    sha = f.body_sha256(RAW_RESTORE)
    with db.tx():
        db.upsert_imap_message("INBOX", 1, 1, message_id="restore1@example.com", body_sha256=sha)
        db.update_imap_message("INBOX", 1, 1, our_action="ham_restored", our_score=9.0)
        db.set_scan_bookmark("INBOX", 1, 1)
    row = _scan_one(db, acc, monkeypatch, uid=2)
    assert row["pending_learn"] == "ham"
    assert row["our_action"] is None


def test_fake_copy_matches_imapclient_return_value():
    assert RecordingIMAP(existing=_all_existing()).copy([1], "INBOX") is None


# ----- FABLE-CR-012: an 8-bit Message-ID must not erase Subject and From ----


def test_parse_envelope_keeps_subject_and_from_with_8bit_message_id():
    raw = (
        b"From: Sender <sender@example.com>\r\n"
        b"Subject: hello\r\n"
        b"Message-ID: <abc\xc3\xa4@example.de>\r\n"
        b"\r\nbody\r\n"
    )
    msgid, subject, sender = f.parse_envelope(raw)
    assert subject == "hello"
    assert sender == "sender@example.com"
    assert msgid and msgid.endswith("@example.de")
    # Such an ID is stored for display but never sent to an IMAP SEARCH.
    assert not f._search_safe_msgid(msgid)


def test_list_drag_of_8bit_message_id_mail_still_records_the_sender(tmp_path, monkeypatch):
    db = _mk_db(tmp_path)
    acc = _mk_account(actual_name="Test User")
    monkeypatch.setattr(f, "rspamd_learn", lambda *a, **k: "learned")
    raw = (
        b"From: friend@example.com\r\nSubject: hi\r\n"
        b"Message-ID: <x\xc3\xa4@example.de>\r\n\r\nbody\r\n"
    )
    client = RecordingIMAP(
        existing=_all_existing(), search_uids=[3],
        fetch_by_uid={3: {b"BODY[]": raw, b"FLAGS": ()}},
    )
    f.drain_list_allow(client, db, LOG, acc, FMAP)
    assert db.list_get("person", "Test User", "allow") == ["friend@example.com"]
    assert client.moved == [([3], FMAP["inbox"])]


# ----- FABLE-CR-008: oversize Train-Ham still gets its Inbox copy -----------


class _CopyFails(RecordingIMAP):
    def copy(self, uids, dest):
        from imapclient.exceptions import IMAPClientError
        raise IMAPClientError("COPY failed")


def _oversize_train_ham(cls=RecordingIMAP):
    # No body stored: RecordingIMAP raises if a BODY fetch is attempted
    # for an oversize UID, so this also proves the body is never pulled.
    return cls(
        existing=_all_existing(),
        search_uids=[7],
        fetch_by_uid={7: {b"RFC822.SIZE": f.MAX_FETCH_BYTES + 1, b"FLAGS": ()}},
    )


def test_oversize_train_ham_is_copied_to_inbox_then_archived(tmp_path, monkeypatch):
    db = _mk_db(tmp_path)
    acc = _mk_account()
    monkeypatch.setattr(f, "rspamd_learn", lambda *a, **k: pytest.fail("no learn"))
    client = _oversize_train_ham()
    f.drain_train_ham(client, db, LOG, acc, FMAP)
    assert client.copied == [([7], FMAP["inbox"])]
    assert client.moved == [([7], FMAP["trained_ham"])]
    assert db.get_imap_message(FMAP["ham_train"], 1, 7)["our_action"] == "inbox_copied"


def test_oversize_train_ham_stays_put_when_the_copy_fails(tmp_path, monkeypatch):
    db = _mk_db(tmp_path)
    acc = _mk_account()
    monkeypatch.setattr(f, "rspamd_learn", lambda *a, **k: pytest.fail("no learn"))
    client = _oversize_train_ham(_CopyFails)
    f.drain_train_ham(client, db, LOG, acc, FMAP)
    assert client.moved == []


def test_ordinary_train_ham_stays_put_when_the_copy_fails(tmp_path, monkeypatch):
    db = _mk_db(tmp_path)
    acc = _mk_account()
    monkeypatch.setattr(f, "rspamd_learn", lambda *a, **k: pytest.fail("no learn"))
    client = _CopyFails(
        existing=_all_existing(), search_uids=[7],
        fetch_by_uid={7: {b"BODY[]": RAW_RESTORE, b"FLAGS": ()}},
    )
    f.drain_train_ham(client, db, LOG, acc, FMAP)
    assert client.moved == []
    assert db.get_imap_message(FMAP["ham_train"], 1, 7)["our_action"] is None


def test_oversize_train_spam_is_still_archived_without_a_copy(tmp_path, monkeypatch):
    db = _mk_db(tmp_path)
    acc = _mk_account()
    monkeypatch.setattr(f, "rspamd_learn", lambda *a, **k: pytest.fail("no learn"))
    client = _oversize_train_ham()
    f.drain_train_spam(client, db, LOG, acc, FMAP)
    assert client.copied == []
    assert client.moved == [([7], FMAP["trained_spam"])]


# ----- FABLE-CR-013: YAML errors must not echo accounts.yml lines -----------


@pytest.mark.parametrize("bad_line", [
    '    password: "S3cretSentinel\n',       # unclosed quote
    "    password: S3cretSentinel: x\n",     # mapping value not allowed
])
def test_invalid_yaml_does_not_echo_the_password_line(tmp_path, bad_line):
    path = tmp_path / "accounts.yml"
    path.write_text(
        "accounts:\n  - name: a\n    imap_host: h\n" + bad_line
        + "    user: u@x.com\n    actual_name: A\n"
    )
    for loader in (f.load_accounts, f.yaml_max_list_entries):
        with pytest.raises(f.ConfigError) as info:
            loader(path)
        assert "S3cretSentinel" not in str(info.value)
        assert "invalid YAML at line" in str(info.value)
        assert info.value.__cause__ is None
        assert info.value.__suppress_context__


def test_unreadable_accounts_file_is_a_config_error(tmp_path):
    with pytest.raises(f.ConfigError):
        f.load_accounts(tmp_path / "missing.yml")


# ----- FABLE-CR-023: a backed-off list drag is not re-fetched every pass ----


class _BodyFetchLog(RecordingIMAP):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.body_fetches: list[list[int]] = []

    def fetch(self, uids, parts):
        names = [p if isinstance(p, bytes) else str(p).encode() for p in parts]
        if any(p in (b"BODY[]", b"BODY.PEEK[]") for p in names):
            self.body_fetches.append(list(uids))
        return super().fetch(uids, parts)


def test_list_drag_waiting_on_learn_budget_is_not_refetched(tmp_path, monkeypatch):
    db = _mk_db(tmp_path)
    acc = _mk_account(actual_name="Test User", max_learns_per_hour=1)
    with db.tx():
        db.record_rate("learn")  # hourly budget already spent
    monkeypatch.setattr(f, "rspamd_learn", lambda *a, **k: pytest.fail("no learn"))
    raw = b"From: friend@example.com\r\nSubject: hi\r\nMessage-ID: <d1@x>\r\n\r\nb\r\n"
    client = _BodyFetchLog(
        existing=_all_existing(), search_uids=[3],
        fetch_by_uid={3: {b"BODY[]": raw, b"FLAGS": ()}},
    )
    f.drain_list_allow(client, db, LOG, acc, FMAP)
    f.drain_list_allow(client, db, LOG, acc, FMAP)
    # The entry is written at once (IMAP drags persist immediately) ...
    assert db.list_get("person", "Test User", "allow") == ["friend@example.com"]
    # ... the message waits in the folder for its retry time ...
    assert client.moved == []
    assert client.body_fetches == [[3]]
    # ... and the Events page gets one add, not one per pass.
    adds = db.conn.execute(
        "SELECT COUNT(*) FROM events WHERE event='list_imap_add'"
    ).fetchone()[0]
    assert adds == 1
