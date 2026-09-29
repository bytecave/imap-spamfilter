# Code and security review orientation

**Written:** 2026-09-28 evening Pacific  
**For:** the next agent doing a full code and security review of this filter  
**Then:** the Outlook add-in. Its requirements are in `outlook-addin/`; the add-in itself is not built yet.

Read [`IMPLEMENTATION_STATUS.md`](IMPLEMENTATION_STATUS.md) and [`SESSION_HANDOFF.md`](SESSION_HANDOFF.md) before this file if you have not already. Those two are the product map. This file only says how to review.

## What this review is

A read-only pass over the filter as it runs on ByteLord: Python IMAP worker, dashboard, rspamd local config, and the deploy files that put them there. Report defects, unsafe defaults, and places where the code does not match the policy in `IMPLEMENTATION_STATUS.md`. Do not fix them in the same pass unless the operator asks.

The Outlook add-in is the work that comes **after** this review. `outlook-addin/outlook_spam_addin_requirements.md` describes a classic-Outlook VSTO ribbon that only moves or copies messages into the existing Train-*, Allowlist, and Blocklist folders. It does not call this filter. There is no add-in project, manifest, or installer in the tree yet. A missing binary is not a finding. Do not start the add-in during the review, and do not review those notes as if they were running code.

## Do not do these things

- Do not wipe Bayes, flush Redis, or delete `NEURAL_*` / Bayes keys. Restore point, if ever needed: `/opt/bytelord/data/imap-spamfilter/redis/dump.rdb.bak-20260928-before-rich-move` and `appendonlydir.bak-20260928-before-rich-move`. AOF is on; a restore needs Redis stopped and both pieces.
- Do not `docker compose down` the Redis container.
- Do not change `mode` on any account. `rich_bytecave` is the only `mode: move` mailbox. The other nine stay `shadow`.
- Do not rebuild or restart `spamfilter`, rspamd, or the proxy unless the operator asks. The running image was built at 19:22 Pacific and already contains this evening's code.
- Do not move, delete, expunge, or learn live mail as part of the review.
- Do not print secrets. `accounts.yml` is gitignored and holds the proxy password (a dummy `LOGIN` password, not the Microsoft token). Do not `cat` it, the secrets env file, or Redis `requirepass`. Do not paste message bodies from live mailboxes into the report; a Message-ID, score, and symbol list is enough.
- Do not commit unless the operator asks.

## How the live system differs from a fresh checkout

| Fact | Where it lives |
|---|---|
| Ten M365 mailboxes through `email-oauth2-proxy` IMAP `LOGIN` on port 1993, `tls_mode: none` on the Docker network | gitignored `accounts.yml` |
| `flag_untrained_junk: true` for every account, via `defaults` | gitignored `accounts.yml`. Builtin default in code is **false** |
| `@kickstarlaunch.com` domain block on all four roster domains | SQLite `/opt/bytelord/data/imap-spamfilter/state/spamfilter.db`, not git |
| `word_dots = false` | `rspamd/local.d/url_suspect.conf`, also copied into the data `local.d` |
| Shared Bayes user `bytelord` | `defaults.bayes_user` |
| Dashboard on loopback `127.0.0.1:8099`, public `https://spam.bytelord.net` | compose + Caddy, outside this review's write scope |

`accounts.yml` is loaded once at process start. A YAML edit needs a `spamfilter` restart, which you should not do. List rows are read from SQLite on each scan.

## Behavior that is easy to misread

**Score moves do not train.** Inbox score ≥ 8 (`threshold`) moves to Junk only in `move` mode, with no spam learn. Provider Junk under 4 (`rescue_below`) moves back to Inbox only in `move` mode, with no ham learn. Mid-band 4–8 stays. Shadow logs both and does not move.

**List hits override routing and do not Bayes-learn by themselves.** A block still forces Junk after a Train-Ham restore. An allow still beats a block only on a true tie at the same rank. `@host` matches that host and its subdomains; the longer host wins inside one rank.

**Train-Ham is a copy, then an archive.** `drain_train_ham` copies the message to the Inbox before `try_learn`, records SHA-256 of the unchanged bytes (`inbox_copied` / `ham_restored`), then the learn MOVEs the Train-Ham copy to Trained-Ham. `scan_inbox` still stores the score and does not queue Junk for that fingerprint. Historical ham without `inbox_copied` is not held. Train-Spam does not copy to the Inbox. The filter never edits the RFC822 body to "mark" it.

**`\Flagged` is the only user-visible IMAP mark Exchange gives us.** It is used in three different places: over-threshold Inbox in `flag` mode, allowlisted spoof suspects, and (when `flag_untrained_junk` is on) new untrained Junk. A custom keyword or a header rewrite is not a substitute: Outlook will not show a keyword in the folder list, and rewriting the message changes the body hash the filter uses as identity.

**Exchange MOVE is often COPY.** Identity is a SHA-256 of the message bytes, not Message-ID. `_identical_copies_in_folder` is the check before deleting a leftover. Train-* drains expunge a verified leftover. CR-014, the same leftover after an Inbox→Junk score move, is still open.

**Dashboard class "spam"** is a filter action (`pending_move`, `moved_to_junk`, `flagged`, `shadow`, `blocklisted`, `move`, `tag`) or an empty action with `learned_as=spam`. It does not mean "this row is sitting in Junk," and it does not mean `BAYES_SPAM` fired. `ham_restored` is not in that set. `BAYES_SPAM` is capped near +5.1, so more Train-Spam of the same campaign will not push that symbol higher.

**Microsoft auth trust (bucket B).** DKIM/SPF/DMARC/BLACKLIST_DMARC failure weight is zeroed only when the outermost Authentication-Results is Microsoft (`mx.microsoft.com`, or `compauth=` plus a `Received-SPF` receiver of `protection.outlook.com`) and that check passed. A real fail, or a non-Microsoft outer result, keeps the weight. Do not recommend a blanket auth disable.

**Bookmarks skip history.** The first time a folder is seen, the filter records the max UID and does not scan older mail. That is why turning on `flag_untrained_junk` did not flag Junk that was already there, and why a COPY into the Inbox gets a new UID and is scored.

## Suggested review order

1. `filter/filter.py` — account loop, `scan_inbox`, `poll_junk`, `drain_train_ham` / `_restore_train_ham_to_inbox`, list classify, `_move_clearing_source`, learn budget, retention.
2. `filter/dashboard.py` — auth, CSRF, what a browser session can change (lists, modes if exposed), the message query.
3. `filter/test_core_review_fixes.py`, `filter/test_shadow_mode.py`, `filter/test_opus_review_fixes.py`, `filter/test_connection.py` — the claims the new behavior depends on.
4. `rspamd/local.d/` — actions are cosmetic; `url_suspect.conf`; multimap / group files that zero bucket A.
5. `deploy/bytelord-compose.yaml` — ports, mounts, which files are live. The running compose is `/opt/bytelord/compose/imap-spamfilter/compose.yaml` and does not auto-sync from git.
6. Prior reviews, so you do not re-file closed items as new: `CLAUDE_OPUS5.5_EXTRA_CODE_FIXED.md`, `CLAUDE_OPUS5.5_EXTRA_CODE_REVIEW.md`, `CHATGPT_CODE_REVIEW.md`.

## Open on purpose

- CR-014: Inbox→Junk score-move leftover when Exchange turns MOVE into COPY.
- CR-019: rspamd `Rcpt` is the mailbox address, not the first To/Cc.
- `DASHBOARD_TRUSTED_PROXIES` is unset; the dashboard trusts the immediate peer.
- ChatGPT CR-016 supply chain (locked deps, image digests, action SHA pins) was accepted risk.
- Neural stays off. Do not propose turning it on as a review fix. The reasons are in `IMPLEMENTATION_STATUS.md`.
- Trained-* retention is on for `rich_bytecave` at the default of 7 days. Do not change it as part of the review.

## How to run tests

Host pytest is not the suite. From `/opt/bytelord/projects/imap-spamfilter`:

```bash
docker run --rm -v "$PWD/filter":/src/filter -w /src/filter python:3.12-slim \
  sh -c 'pip install -q pytest==8.4.2 && python -m pytest -q'
```

Last full run before this document: 429 passed. A review that adds no code does not need to re-run them.

## What a useful report looks like

Group findings by severity. For each one, name the function, what an attacker or a normal mailbox can make it do, and whether it is reachable in shadow, in move mode, or only from the dashboard. Separate "does not match the written policy" from "policy I would change." Do not include a patch unless the operator asks for one.
