"""One-shot CLI to bulk-train Bayes from an existing IMAP folder.

Reads every message from a source folder, sends it to rspamd's /learn{spam,ham}
endpoint, and (optionally) moves trained messages to a destination folder.
Intended for cold-start: prepare a folder with known spam (or known ham),
run this once, then delete the folder. Never deletes messages itself.

Usage (inside the container):
    python bootstrap_train.py <account_name> <source_folder> <spam|ham> \\
        [--move-to FOLDER] [--move-declined] [--limit N] [--dry-run]
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from imapclient.exceptions import IMAPClientError

from filter import (
    CONFIG_PATH,
    RSPAMD_PASSWORD,
    connect_imap,
    detect_delimiter,
    fetch_under_cap,
    load_accounts,
    parse_envelope,
    resolve_folder,
    rspamd_learn,
)


def positive_limit(value: str) -> int:
    """argparse type for a bounded, non-empty training selection."""
    try:
        limit = int(value)
    except ValueError as ex:
        raise argparse.ArgumentTypeError("limit must be an integer") from ex
    if limit <= 0:
        raise argparse.ArgumentTypeError("limit must be greater than zero")
    return limit


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("account", help="account name from accounts.yml")
    p.add_argument("source", help="source IMAP folder (e.g. INBOX, Bootstrap-Spam)")
    p.add_argument("kind", choices=["spam", "ham"])
    p.add_argument("--move-to", help="after learning, MOVE message to this folder")
    p.add_argument(
        "--move-declined",
        action="store_true",
        help="also move messages rspamd declined (requires --move-to)",
    )
    p.add_argument("--limit", type=positive_limit, default=10_000)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--config", default=str(CONFIG_PATH))
    args = p.parse_args(argv)
    if args.move_declined and not args.move_to:
        p.error("--move-declined requires --move-to")

    if not RSPAMD_PASSWORD:
        print("RSPAMD_PASSWORD env var is unset", file=sys.stderr)
        return 2

    accounts = load_accounts(Path(args.config))
    try:
        acc = next(a for a in accounts if a.name == args.account)
    except StopIteration:
        print(f"unknown account: {args.account}", file=sys.stderr)
        return 2

    client = None
    try:
        client = connect_imap(acc)
        delim = detect_delimiter(client)
        src = resolve_folder(args.source, delim)
        dst = resolve_folder(args.move_to, delim) if args.move_to else None

        info = client.select_folder(src, readonly=args.dry_run)
        print(f"selected {src} ({info.get(b'EXISTS', '?')} messages)")
        uids = client.search(["ALL"])
        uids = uids[: args.limit]
        if not uids:
            print("nothing to do")
            return 0

        counts = {
            "learned": 0,
            "already": 0,
            "declined": 0,
            "failed": 0,
            "dry_run": 0,
        }
        move_uids: list[int] = []
        processed_uids: set[int] = set()
        t0 = time.time()
        for uid, data, oversize in fetch_under_cap(client, uids):
            processed_uids.add(uid)
            if oversize:
                counts["failed"] += 1
                size = data.get(b"RFC822.SIZE")
                print(f"  FAILED uid={uid} (missing/oversize body, size={size})")
                continue
            raw = data.get(b"BODY[]") or data.get(b"BODY.PEEK[]")
            if not raw:
                counts["failed"] += 1
                print(f"  FAILED uid={uid} (body unavailable)")
                continue
            msgid, subject, _ = parse_envelope(raw)
            short = (subject or "")[:60].replace("\n", " ")
            if args.dry_run:
                print(f"[dry-run] would learn-{args.kind}: uid={uid} subj={short!r}")
                counts["dry_run"] += 1
                continue
            outcome = rspamd_learn(raw, args.kind, user=acc.bayes_user or acc.user)
            if outcome in ("learned", "already", "declined"):
                counts[outcome] += 1
                if outcome in ("learned", "already") or args.move_declined:
                    move_uids.append(uid)
            else:
                counts["failed"] += 1
                print(f"  FAILED uid={uid} msgid={msgid} ({outcome})")
            processed = sum(counts.values())
            if processed % 25 == 0:
                print(f"  processed {processed}/{len(uids)} so far")

        unprocessed = len(set(uids) - processed_uids)
        if unprocessed:
            counts["failed"] += unprocessed
            print(f"  FAILED {unprocessed} uid(s) were not returned by the fetch")

        dt = time.time() - t0
        print(
            "done: "
            f"learned={counts['learned']} "
            f"already={counts['already']} "
            f"declined={counts['declined']} "
            f"failed={counts['failed']} "
            f"dry_run={counts['dry_run']} "
            f"in {dt:.1f}s"
        )

        if dst and move_uids and not args.dry_run:
            try:
                client.move(move_uids, dst)
                print(f"moved {len(move_uids)} -> {dst}")
            except IMAPClientError as ex:
                print(f"move failed: {ex}", file=sys.stderr)
                return 3
        return 1 if counts["failed"] else 0
    finally:
        if client is not None:
            try:
                client.logout()
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(main())
