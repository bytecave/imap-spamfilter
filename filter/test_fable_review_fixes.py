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
