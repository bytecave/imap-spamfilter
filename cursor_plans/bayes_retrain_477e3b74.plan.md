---
name: Bayes retrain
overview: Pause the filter, snapshot Redis, wipe only the shared `bytelord` Bayes notebook, rescore Trained-Spam with the new scorer, move clear ham into Trained-Ham, then relearn every mailbox’s Trained-* folders plus a small allowlisted Google/Amazon ham sample.
todos:
  - id: dry-run-corpus
    content: Dry-run --all-trained and confirm bytelord notebook plus Trained-* counts
    status: pending
  - id: snapshot-wipe
    content: BGSAVE Redis, delete only bytelord Bayes keys, confirm learn counts are 0
    status: pending
  - id: rescore-move
    content: Rescore Trained-Spam; MOVE to Trained-Ham only when score < 6
    status: pending
  - id: relearn
    content: bootstrap_train --all-trained in place; confirm both classes reach min_learns 200
    status: pending
  - id: google-amazon
    content: Score all Inbox Google/Amazon; learn 20 clear-ham of each site-wide
    status: pending
  - id: restart-verify
    content: Start spamfilter in shadow; rescore payroll, Amazon, and one known spam
    status: pending
isProject: false
---

# Retrain the shared Bayes notebook

This is safe. The training set lives in IMAP. Redis Bayes is a copy of it. Fuzzy hashes and neural weights stay. All accounts are `mode: shadow`, so the filter is not auto-moving mail.

There is one notebook, not one per mailbox. Live `defaults.bayes_user` is `bytelord`. Relearning each mailbox still writes into that same notebook. Old per-address Redis keys are already unused; leave them.

## How long Trained-* is kept

Configured retention is **7 days** for both `Trained-Spam` and `Trained-Ham` ([README.md](README.md) `trained_retention_days`, [filter/filter.py](filter/filter.py) `BUILTIN_DEFAULTS`). `retention_sweep` returns immediately in shadow (`mode_allows_retention` is false). While every account stays in shadow, those folders are not moved to Trash. They have been accumulating. The 7-day clock starts only after a mailbox is promoted to `flag` or `move`.

## What “ham” means for the move

Rspamd actions: greylist 4, add header 6, reject 15. A Trained-Spam message moves to Trained-Ham only when the **post-wipe** score is **under 6**. Score 6 or higher stays in Trained-Spam and is relearned as spam. That keeps the gray band on the label you already gave it.

## Pause

Stop only the `spamfilter` container (scan loop, Train-* drains, and the dashboard). Leave `spamfilter-rspamd`, `spamfilter-redis`, and Unbound running. One-shot commands use `docker compose run --rm --no-deps spamfilter` so the loop is not also learning. Do not `compose down` Redis.

## Steps

1. **Confirm corpus, do not learn yet.** `docker compose run --rm --no-deps spamfilter python bootstrap_train.py --all-trained --dry-run` prints per-account folder names and counts. Also print live `trained_retention_days` and `bayes_user` from `accounts.yml` (no secrets in the output). Abort if either Trained folder is empty across the site, or if `bayes_user` is not `bytelord`.
2. **Snapshot Redis.** `BGSAVE` on `spamfilter-redis` and copy the dump aside. Redis also holds fuzzy and neural data ([README.md](README.md) persistent-data section). Never `FLUSHDB` / `FLUSHALL`.
3. **Delete only `bytelord` Bayes keys.** `SCAN` first and record the key families. Delete the Bayes token and learn-counter keys for user `bytelord` only. Confirm a stat read shows ham and spam learns at 0 for that user. Leave every other prefix.
4. **Rescore Trained-Spam before any learn.** For each account, fetch `Junk/Trained-Spam` (SPECIAL-USE name from the dry-run) and score with the rebuilt filter (`Delivered-To: bytelord`, real mailbox `Rcpt`, Microsoft AR trust). Write a report: account, UID, from, subject, score, top symbols. Move UID to Trained-Ham only when score < 6. Do not delete anything.
5. **Relearn Trained-*.** `bootstrap_train.py --all-trained` learns in place (no `--move-to`) into `acc.bayes_user`. Allowlisted mail in Trained-Spam is skipped by `list_blocks_learn` (allow + spam is refused). Those skips are expected; the move in step 4 is what gets a clear-ham allowlisted message into the ham class. Require `min_learns` (200) on both classes before treating Bayes as live. `expire = 0` and `autolearn = false` stay as they are.
6. **Google and Amazon ham sample.** Score every Inbox message from `google.com` and `amazon.com` (read-only) and keep that report so other scoring bugs show up. Learn as ham, in place, no MOVE, only messages that score under 6, capped at **20 Google and 20 Amazon for the whole site** (spread across mailboxes, skip a Message-ID already learned). Learning every allowlisted copy would overweight one sender in the single notebook. Allow + ham is permitted; block + ham is not.
7. **Start `spamfilter` again.** Accounts stay `shadow`. Rescore payroll UID 140596 and Amazon UID 235843. Bayes should contribute. Auth symbols stay suppressed. `BROKEN_HEADERS` stays off those two. Spot-check one known spam from Trained-Spam and confirm it still scores high.
8. **Docs.** Note the wipe, the move rule, the sample cap, and the new learn counts in [SESSION_HANDOFF.md](SESSION_HANDOFF.md) and [IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md). No commit unless you ask.

## Rollback

Stop `spamfilter`, restore the Redis snapshot, start `spamfilter`. IMAP Trained-* moves from step 4 are the only mailbox writes; move those UIDs back only if the report says the move was wrong. The snapshot is the Bayes rollback.