"""One-shot CLI to bulk-train Bayes from an existing IMAP folder.

Reads every message from a source folder, sends it to rspamd's /learn{spam,ham}
endpoint, and (optionally) moves trained messages to a destination folder.
Intended for cold-start: prepare a folder with known spam (or known ham),
run this once, then delete the folder. Never deletes messages itself.

Usage (inside the container):
    python bootstrap_train.py <account_name> <source_folder> <spam|ham> \\
        [--move-to FOLDER] [--limit N] [--dry-run]
"""

from __future__ import annotations

import argparse
import ssl
import sys
import time
from pathlib import Path

from imapclient import IMAPClient
from imapclient.exceptions import IMAPClientError

from filter import (
    CONFIG_PATH,
    RSPAMD_PASSWORD,
    build_folder_map,
    detect_delimiter,
    load_accounts,
    parse_envelope,
    resolve_folder,
    rspamd_learn,
)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("account", help="account name from accounts.yml")
    p.add_argument("source", help="source IMAP folder (e.g. INBOX, Bootstrap-Spam)")
    p.add_argument("kind", choices=["spam", "ham"])
    p.add_argument("--move-to", help="after learning, MOVE message to this folder")
    p.add_argument("--limit", type=int, default=10_000)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--config", default=str(CONFIG_PATH))
    args = p.parse_args()

    if not RSPAMD_PASSWORD:
        print("RSPAMD_PASSWORD env var is unset", file=sys.stderr)
        return 2

    accounts = load_accounts(Path(args.config))
    try:
        acc = next(a for a in accounts if a.name == args.account)
    except StopIteration:
        print(f"unknown account: {args.account}", file=sys.stderr)
        return 2

    client: IMAPClient | None = None
    try:
        ssl_ctx = ssl.create_default_context() if acc.ssl else None
        client = IMAPClient(
            acc.imap_host,
            port=acc.imap_port,
            ssl=acc.ssl,
            ssl_context=ssl_ctx,
            timeout=60,
        )
        client.login(acc.user, acc.password)
        delim = detect_delimiter(client)
        fmap = build_folder_map(acc, delim)
        src = resolve_folder(args.source, delim)
        dst = resolve_folder(args.move_to, delim) if args.move_to else None

        info = client.select_folder(src, readonly=args.dry_run)
        print(f"selected {src} ({info.get(b'EXISTS', '?')} messages)")
        uids = client.search(["ALL"])
        uids = uids[: args.limit]
        if not uids:
            print("nothing to do")
            return 0

        ok_count = 0
        fail_count = 0
        learned_uids: list[int] = []
        t0 = time.time()
        fetched = client.fetch(uids, [b"BODY.PEEK[]"])
        for uid, data in fetched.items():
            raw = data.get(b"BODY[]") or data.get(b"BODY.PEEK[]")
            if not raw:
                fail_count += 1
                continue
            msgid, subject, _ = parse_envelope(raw)
            short = (subject or "")[:60].replace("\n", " ")
            if args.dry_run:
                print(f"[dry-run] would learn-{args.kind}: uid={uid} subj={short!r}")
                ok_count += 1
                continue
            outcome = rspamd_learn(raw, args.kind, user=acc.bayes_user or acc.user)
            if outcome in ("learned", "already", "declined"):
                ok_count += 1
                learned_uids.append(uid)
                if ok_count % 25 == 0:
                    print(f"  learned {ok_count}/{len(uids)} so far")
            else:
                fail_count += 1
                print(f"  FAILED uid={uid} msgid={msgid} ({outcome})")

        dt = time.time() - t0
        print(f"done: learned={ok_count} failed={fail_count} in {dt:.1f}s")

        if dst and learned_uids and not args.dry_run:
            try:
                client.move(learned_uids, dst)
                print(f"moved {len(learned_uids)} -> {dst}")
            except IMAPClientError as ex:
                print(f"move failed: {ex}", file=sys.stderr)
                return 3
        return 0
    finally:
        if client is not None:
            try:
                client.logout()
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(main())
