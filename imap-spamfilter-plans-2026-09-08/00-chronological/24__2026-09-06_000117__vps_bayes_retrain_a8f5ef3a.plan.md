<!--
  ARCHIVE METADATA (prepended for chronological review; not in original source)
  Created:        2026-09-06 00:01:17 -0700
  Last modified:  2026-09-06 00:24:38 -0700
  Created source: filesystem birth/mtime
  Category:       cursor-plans
  Original name:  vps_bayes_retrain_a8f5ef3a.plan.md
  Archive name:   24__2026-09-06_000117__vps_bayes_retrain_a8f5ef3a.plan.md
  Source path:    /home/bytecave/.cursor/plans/vps_bayes_retrain_a8f5ef3a.plan.md
  Notes:          Cursor plan; not in git. Timestamp from file birth time when available, else mtime.
-->

> **Created:** 2026-09-06 00:01:17 -0700  
> **Last modified:** 2026-09-06 00:24:38 -0700  
> **Original filename:** `vps_bayes_retrain_a8f5ef3a.plan.md`  
> **Source:** `/home/bytecave/.cursor/plans/vps_bayes_retrain_a8f5ef3a.plan.md`

---
---
name: VPS Bayes retrain
overview: Do not MOVE mail back to Train-*. Re-learn every account’s Trained-Spam/Ham in place into the shared `bytelord` notebook via an all-accounts wrapper around bootstrap_train.py. Rspamd HTTP 208 already skips messages that are already in that notebook.
todos:
  - id: cli-all-trained
    content: "Add bootstrap_train.py --all-trained: SPECIAL-USE remap, Trained-* in place, no MOVE, per-account totals"
    status: completed
  - id: tests
    content: Extend test_bootstrap_train.py for --all-trained, skip missing folders, already counts
    status: completed
  - id: docs-ship
    content: Document README + IMPLEMENTATION_STATUS; pytest; commit/push
    status: completed
isProject: false
---

# Retrain the shared VPS Bayes notebook from Trained-*

**Do not move mail back into Train-*.** That is more IMAP churn for the same tokens: the live filter would drain Train-*, learn, then MOVE back to Trained-*, capped by `max_train_per_run` and `max_learns_per_hour`. Slice 9 already chose the better path: learn **in place** from Trained-* and omit `--move-to`.

Stay in **shadow** until this re-feed finishes (retention does not trash Trained-* in shadow).

## Duplicate learns (your ID question)

Rspamd does **not** key off IMAP UID or Message-ID. It hashes the message **body** inside a Bayes **user** (`Delivered-To`, here `bytelord` from `defaults.bayes_user`).

[`rspamd_learn`](filter/filter.py) already classifies:

- `learned` (HTTP 200) — new tokens in **this** notebook
- `already` (HTTP 208) — same body already in this class for `bytelord`; **no second train**
- `declined` (HTTP 204) — too few tokens or otherwise not learned; treated as terminal, not retried

So:

- Mail already learned into `bytelord` (Train-* drain after the shared notebook went live) → **208 / already**, skipped.
- Same body in two mailboxes’ Trained-Ham → first account **200**, second **208**.
- Mail learned only under the **old per-mailbox** Redis keys (`rich@bytecave.net`, etc.) → **200 into `bytelord`**. That is the point of the re-feed; old keys are not consulted and are not merged.

No extra SQLite/IMAP identity layer is needed.

## What to build

Extend existing [`filter/bootstrap_train.py`](filter/bootstrap_train.py) (already in the image; dry-run, `--limit`, in-place learn, `learned`/`already` counts). Add an **all-accounts** mode rather than a throwaway app:

- `python bootstrap_train.py --all-trained` (optional `--kind spam|ham`, default both)
- Load [`accounts.yml`](accounts.yml) via `load_accounts`
- Per account: `connect_imap`, same SPECIAL-USE junk remap as [`account_loop`](filter/filter.py) (~L3418), then `build_folder_map` so M365 `Junk Email/Trained-Spam` is used automatically
- SELECT each Trained-* **readonly**; skip missing/empty folders
- Call existing `rspamd_learn(raw, kind, user=acc.bayes_user or acc.user)` — **no `--move-to`**
- Print per-account and grand totals: `learned` / `already` / `declined` / `failed`
- Keep `--dry-run` and `--limit` (limit per folder)

IMAP drag / Train-* drain stay as they are. Filter can keep running: overlapping learns are 208. Direct controller POSTs **bypass** `max_learns_per_hour` (intentional for this bulk job).

## Tests

Extend [`filter/test_bootstrap_train.py`](filter/test_bootstrap_train.py): `--all-trained` walks two fake accounts, both kinds, no MOVE; missing folder is skipped not failed; `already` increments the already counter.

## How we will run it on the VPS

Rebuild `spamfilter` so the CLI is in the image, then:

```bash
docker exec spamfilter python bootstrap_train.py --all-trained --dry-run
docker exec spamfilter python bootstrap_train.py --all-trained
```

Watch totals: `learned` should rise toward a useful notebook (`min_learns = 200` spam **and** 200 ham per notebook). `already` is expected for anything already in `bytelord`.

## Docs

Short README + [`IMPLEMENTATION_STATUS.md`](IMPLEMENTATION_STATUS.md) note: re-feed is `--all-trained`, not Train-* round-trip. Commit/push after tests; rebuild only when you ask to run it live (or in the same session if you want it executed immediately after merge).