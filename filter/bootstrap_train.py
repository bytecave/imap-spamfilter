"""One-shot CLI to bulk-train Bayes from an existing IMAP folder.

Reads every message from a source folder, sends it to rspamd's /learn{spam,ham}
endpoint, and (optionally) moves trained messages to a destination folder.
Intended for cold-start: prepare a folder with known spam (or known ham),
run this once, then delete the folder. Never deletes messages itself.

Usage (inside the container):
    python bootstrap_train.py <account_name> <source_folder> <spam|ham> \\
        [--move-to FOLDER] [--move-declined] [--limit N] [--dry-run]

    python bootstrap_train.py --all-trained [--kind spam|ham] \\
        [--limit N] [--dry-run]

--all-trained re-learns every account's Trained-Spam / Trained-Ham in
place (no MOVE) into acc.bayes_user or acc.user. Missing folders are
skipped. Rspamd HTTP 208 counts as already (no second train).
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any

from imapclient.exceptions import IMAPClientError

from filter import (
    CONFIG_PATH,
    RSPAMD_PASSWORD,
    apply_special_use_remap,
    build_folder_map,
    connect_imap,
    detect_delimiter,
    fetch_under_cap,
    load_accounts,
    parse_envelope,
    resolve_folder,
    rspamd_learn,
)

COUNT_KEYS = ("learned", "already", "declined", "failed", "dry_run")


def positive_limit(value: str) -> int:
    """argparse type for a bounded, non-empty training selection."""
    try:
        limit = int(value)
    except ValueError as ex:
        raise argparse.ArgumentTypeError("limit must be an integer") from ex
    if limit <= 0:
        raise argparse.ArgumentTypeError("limit must be greater than zero")
    return limit


def _empty_counts() -> dict[str, int]:
    return {k: 0 for k in COUNT_KEYS}


def _add_counts(dst: dict[str, int], src: dict[str, int]) -> None:
    for k in COUNT_KEYS:
        dst[k] += src[k]


def _format_counts(counts: dict[str, int], *, seconds: float | None = None) -> str:
    parts = " ".join(f"{k}={counts[k]}" for k in COUNT_KEYS)
    if seconds is None:
        return parts
    return f"{parts} in {seconds:.1f}s"


def train_folder(
    client: Any,
    acc: Any,
    *,
    src: str,
    kind: str,
    dry_run: bool,
    limit: int,
    move_to: str | None = None,
    move_declined: bool = False,
    readonly: bool = False,
) -> tuple[dict[str, int], bool]:
    """Learn (and optionally MOVE) messages in one IMAP folder.

    Returns (counts, skipped). skipped is True when SELECT fails (folder
    missing); that is not a learn failure.
    """
    counts = _empty_counts()
    try:
        info = client.select_folder(src, readonly=readonly or dry_run)
    except IMAPClientError as ex:
        print(f"  skip {src}: {ex}")
        return counts, True
    exists = info.get(b"EXISTS", "?")
    print(f"selected {src} ({exists} messages)")
    uids = list(client.search(["ALL"]) or [])
    uids = uids[:limit]
    if not uids:
        print("nothing to do")
        return counts, False

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
        if dry_run:
            print(f"[dry-run] would learn-{kind}: uid={uid} subj={short!r}")
            counts["dry_run"] += 1
            continue
        outcome = rspamd_learn(raw, kind, user=acc.bayes_user or acc.user)
        if outcome in ("learned", "already", "declined"):
            counts[outcome] += 1
            if outcome in ("learned", "already") or move_declined:
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

    print("done: " + _format_counts(counts, seconds=time.time() - t0))

    if move_to and move_uids and not dry_run:
        try:
            client.move(move_uids, move_to)
            print(f"moved {len(move_uids)} -> {move_to}")
        except IMAPClientError as ex:
            print(f"move failed: {ex}", file=sys.stderr)
            counts["failed"] += 1
    return counts, False


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--all-trained",
        action="store_true",
        help="re-learn Trained-Spam/Ham in place for every account",
    )
    p.add_argument(
        "--kind",
        dest="all_kind",
        choices=["spam", "ham"],
        help="with --all-trained, only this class (default both)",
    )
    p.add_argument("account", nargs="?", help="account name from accounts.yml")
    p.add_argument("source", nargs="?", help="source IMAP folder (e.g. INBOX)")
    p.add_argument("kind", nargs="?", choices=["spam", "ham"])
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
    if args.all_trained:
        if args.account or args.source or args.kind:
            p.error("--all-trained does not take account/source/kind arguments")
        if args.move_to or args.move_declined:
            p.error("--all-trained learns in place; do not pass --move-to")
    else:
        if args.all_kind:
            p.error("--kind is only valid with --all-trained")
        if not args.account or not args.source or not args.kind:
            p.error("account, source folder, and kind are required "
                    "(or pass --all-trained)")
    return args


def _run_one_account(args: argparse.Namespace, acc: Any) -> int:
    client = None
    try:
        client = connect_imap(acc)
        delim = detect_delimiter(client)
        src = resolve_folder(args.source, delim)
        dst = resolve_folder(args.move_to, delim) if args.move_to else None
        counts, _skipped = train_folder(
            client, acc,
            src=src, kind=args.kind,
            dry_run=args.dry_run, limit=args.limit,
            move_to=dst, move_declined=args.move_declined,
            readonly=args.dry_run,
        )
        return 1 if counts["failed"] else 0
    finally:
        if client is not None:
            try:
                client.logout()
            except Exception:
                pass


def _run_all_trained(args: argparse.Namespace, accounts: list[Any]) -> int:
    kinds = (args.all_kind,) if args.all_kind else ("spam", "ham")
    fmap_key = {"spam": "trained_spam", "ham": "trained_ham"}
    totals = _empty_counts()
    skipped_folders = 0
    any_failed = False
    for acc in accounts:
        client = None
        try:
            client = connect_imap(acc)
            delim = detect_delimiter(client)
            apply_special_use_remap(acc, client)
            fmap = build_folder_map(acc, delim)
            print(
                f"[{acc.name}] bayes_user={acc.bayes_user or acc.user} "
                f"trained_spam={fmap['trained_spam']} "
                f"trained_ham={fmap['trained_ham']}"
            )
            for kind in kinds:
                src = fmap[fmap_key[kind]]
                counts, skipped = train_folder(
                    client, acc,
                    src=src, kind=kind,
                    dry_run=args.dry_run, limit=args.limit,
                    readonly=True,
                )
                if skipped:
                    skipped_folders += 1
                    continue
                _add_counts(totals, counts)
                if counts["failed"]:
                    any_failed = True
        except Exception as ex:
            any_failed = True
            print(f"[{acc.name}] FAILED: {ex}", file=sys.stderr)
        finally:
            if client is not None:
                try:
                    client.logout()
                except Exception:
                    pass
    print(
        "all-trained totals: "
        + _format_counts(totals)
        + f" skipped_folders={skipped_folders}"
    )
    return 1 if any_failed else 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    if not RSPAMD_PASSWORD:
        print("RSPAMD_PASSWORD env var is unset", file=sys.stderr)
        return 2

    accounts = load_accounts(Path(args.config))
    if args.all_trained:
        return _run_all_trained(args, accounts)

    try:
        acc = next(a for a in accounts if a.name == args.account)
    except StopIteration:
        print(f"unknown account: {args.account}", file=sys.stderr)
        return 2
    return _run_one_account(args, acc)


if __name__ == "__main__":
    sys.exit(main())
