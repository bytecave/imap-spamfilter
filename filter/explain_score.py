"""Re-scan one IMAP message and print rspamd symbol contributions.

Uses the same /checkv2 headers as the live filter (From + Rcpt / bayes_user).
Does not learn or move mail.

Usage (inside the container):
    python explain_score.py <account> --uid 234134
    python explain_score.py <account> --uid 234134 --folder INBOX
    python explain_score.py <account> --message-id '<id@amazon.com>'
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from imapclient.exceptions import IMAPClientError

from filter import (
    CONFIG_PATH,
    Db,
    apply_special_use_remap,
    build_folder_map,
    connect_imap,
    detect_delimiter,
    first_recipient,
    format_top_symbols_line,
    init_db,
    load_accounts,
    parse_envelope,
    resolve_folder,
    rspamd_scan_detail,
)


def _fetch_body(client, folder: str, uid: int) -> bytes | None:
    try:
        client.select_folder(folder, readonly=True)
    except IMAPClientError as ex:
        print(f"select {folder!r} failed: {ex}", file=sys.stderr)
        return None
    data = client.fetch([uid], [b"BODY.PEEK[]", b"RFC822.SIZE"])
    row = data.get(uid) or {}
    raw = row.get(b"BODY[]") or row.get(b"BODY.PEEK[]")
    return raw if raw else None


def _lookup_uid_by_msgid(account: str, msgid: str) -> tuple[str, int] | None:
    init_db()
    db = Db(account)
    try:
        bare = msgid.strip().strip("<>")
        rows = (
            db.find_by_message_id(msgid)
            or db.find_by_message_id(bare)
            or db.find_by_message_id(f"<{bare}>")
        )
        if not rows:
            return None
        rows = sorted(
            rows,
            key=lambda r: (
                0 if (r["our_score"] is not None) else 1,
                0 if str(r["folder"]).upper().startswith("INBOX") else 1,
                -(r["last_seen"] or 0),
            ),
        )
        r = rows[0]
        return str(r["folder"]), int(r["uid"])
    finally:
        db.close()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("account", help="account name from accounts.yml")
    p.add_argument("--uid", type=int, help="IMAP UID in --folder")
    p.add_argument(
        "--folder",
        default=None,
        help="IMAP folder (default: account Inbox after SPECIAL-USE remap)",
    )
    p.add_argument(
        "--message-id",
        dest="message_id",
        help="look up folder+UID from SQLite messages table",
    )
    p.add_argument("--config", default=str(CONFIG_PATH))
    args = p.parse_args(argv)
    if (args.uid is None) == (args.message_id is None):
        p.error("pass exactly one of --uid or --message-id")

    accounts = load_accounts(Path(args.config))
    try:
        acc = next(a for a in accounts if a.name == args.account)
    except StopIteration:
        print(f"unknown account: {args.account}", file=sys.stderr)
        return 2

    folder: str | None = args.folder
    uid: int | None = args.uid
    if args.message_id:
        found = _lookup_uid_by_msgid(acc.name, args.message_id)
        if found is None:
            print(f"message-id not in DB: {args.message_id}", file=sys.stderr)
            return 2
        folder, uid = found
        print(f"resolved message-id -> folder={folder!r} uid={uid}")

    assert uid is not None

    client = None
    try:
        client = connect_imap(acc)
        delim = detect_delimiter(client)
        apply_special_use_remap(acc, client)
        fmap = build_folder_map(acc, delim)
        if folder is None:
            folder = fmap["inbox"]
        else:
            folder = resolve_folder(folder, delim)

        raw = _fetch_body(client, folder, uid)
        if not raw:
            print(f"no body for uid={uid} in {folder!r}", file=sys.stderr)
            return 1

        msgid, subject, sender = parse_envelope(raw)
        print(f"account={acc.name} folder={folder!r} uid={uid}")
        print(f"from={sender!r}")
        print(f"subject={(subject or '')[:100]!r}")
        print(f"message-id={msgid!r}")
        print(f"bayes_user={acc.bayes_user or acc.user}")
        print(f"bytes={len(raw)}")

        recipient = first_recipient(raw, acc.user)
        result = rspamd_scan_detail(
            raw, recipient, acc.reject_score_above,
            bayes_user=acc.bayes_user or acc.user,
        )
        if result is None:
            print("scan failed (rspamd unreachable or invalid score)", file=sys.stderr)
            return 1

        names = {s.name for s in result.symbols}
        print(f"score={result.score:.2f} action={result.action!r}")
        if "BAYES_SPAM" in names:
            bayes = "BAYES_SPAM"
        elif "BAYES_HAM" in names:
            bayes = "BAYES_HAM"
        else:
            bayes = "none"
        print(f"bayes: {bayes}")
        print("top: " + format_top_symbols_line(result.symbols, n=8))
        print("--- symbols ---")
        print(f"{'score':>8}  {'symbol':<40} description")
        for s in result.symbols:
            print(f"{s.score:8.2f}  {s.name:<40} {s.description[:70]}")
        return 0
    finally:
        if client is not None:
            try:
                client.logout()
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(main())
