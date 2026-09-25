# Session handoff — imap-spamfilter (ByteLord VPS)

**Last updated:** 2026-09-25 (Claude Opus 5.5 extra code review + fixes)  
**Repo:** `/opt/bytelord/projects/imap-spamfilter`  
**Remote:** `github.com:bytecave/imap-spamfilter.git` (branch `main`)  
**Upstream fork of:** marcelverdult/imap-spamfilter  

---

## Mandatory before doing anything else

A new agent **must** do all three before exploring code or proposing fixes:

1. **Read this file** (where we left off + next steps).
2. **Read [`IMPLEMENTATION_STATUS.md`](IMPLEMENTATION_STATUS.md) in full** — architecture, policy, live VPS, remediation, What’s next. This handoff is the short continuity note; that file is the durable map. If they disagree, **trust `IMPLEMENTATION_STATUS.md` for product facts** and this file for “continue here.”
3. **Search Supermemory** (MCP `plugin-cursor-supermemory-supermemory`, `container=project`) before answering “why / what next / how scoring works.” Do not rely on chat memory. Useful seeds:
   - `IMAP-path remediation buckets A B C mx.microsoft.com`
   - `Bayes retrain bytelord Delivered-To Rcpt`
   - `dashboard Trained rescore score_detail`
   - `imap-spamfilter shadow dashboard 8099`
   Also `supermemory_list` (recent project memories). After decisions or live deploys, `supermemory_add` with `container=project`.

Also read `/home/bytecave/.claude/CLAUDE.md` (Cursor user rule) and use Agent Mail + graphify as that file and `IMPLEMENTATION_STATUS.md` § Agent onboarding require.

---

## Where we left off (2026-09-25) — code review + fixes COMPLETE, awaiting human testing

A full code/security review (IMPLEMENTATION_STATUS "What's next" item 3) was done by Claude Code (Opus 5.5) in a cloud session, and the fixes that passed review are committed.

- **Findings:** [`CLAUDE_OPUS5.5_EXTRA_CODE_REVIEW.md`](CLAUDE_OPUS5.5_EXTRA_CODE_REVIEW.md): 28 traced findings (4 High, 11 Medium, 13 Low).
- **What was fixed:** [`CLAUDE_OPUS5.5_EXTRA_CODE_FIXED.md`](CLAUDE_OPUS5.5_EXTRA_CODE_FIXED.md): 15 fixes, one commit each, plus new tests and README corrections.
- **Git:** everything is on **`main`** (pushed 2026-09-25). `git pull` in `/opt/bytelord/projects/imap-spamfilter` picks it up. **Nothing is deployed live yet.**
- **Tests:** `cd filter && python -m pytest -q` → **401 passed** (was 349). New tests are mostly in `filter/test_opus_review_fixes.py`.
- **Scope of change:** `filter/filter.py`, `filter/dashboard.py`, `unraid/bootstrap.sh`, `deploy/bytelord-compose.yaml` (`TZ: America/Los_Angeles`, `stop_grace_period: 90s`), `README.md`, tests. **No Rspamd/Redis config, `accounts.yml`, or data changes.** `bootstrap.version` is not bumped (no `local.d` file changed).
- **New operator-requested feature:** an **allowlisted** Inbox message that Microsoft's trusted Authentication-Results marks as **spoofed** stays in Inbox but gets a **red follow-up flag** in Outlook (flag/move modes) plus an `allowlisted_spoof_suspect` event. Shadow only logs `[shadow] would flag allowlisted spoof suspect`.
- **Accounts remain `mode: shadow`.** Do not promote because of this review alone.
- This cloud session had no Supermemory, Agent Mail, graphify, or VPS access. The next Cursor session should `supermemory_add` a summary of this review (container=project) and run `graphify update .`.

### Test these first (highest risk / most behavior change)

| # | Fix | What to check on the live system |
|---|---|---|
| 1 | **CR-001** list-drain de-dup (`_identical_copies_in_folder`) | Drag a message from Junk → `INBOX/Allowlist`, and one from Inbox → `INBOX/Blocklist`. Each must land in Inbox/Junk respectively and leave no copy in the list folder. The Exchange leftover cleanup should still log "byte-identical copy ... expunging leftover copies". A duplicate in the destination (instead of a deletion) is the new safe failure mode. |
| 2 | **CR-002** poison give-up (`scan_giveup`) | Stop `spamfilter-rspamd` for more than 10 minutes while mail arrives, then restart it. Expect `scan_failed` → halt → resume, and **no** `scan_giveup` events. Any `giving up on ...` log line or `scan_giveup` event on the Events page deserves a look with `explain_score.py`. |
| 3 | **CR-005** dashboard list Save > 16 KiB | Paste about 700 test addresses into a *test* user list and Save. It must succeed (it used to return 413), then Cancel/remove them. `/login` must still reject huge bodies. |
| 4 | **CR-011** `BEGIN IMMEDIATE` | Watch `docker logs spamfilter` for `database is locked` or `unhandled error in account loop` (should drop), and make sure no worker stalls while the dashboard saves lists. |
| 5 | **CR-013** learn budget before fetch | Drop about 100 messages into one account's Train-Spam. Expect roughly `max_learns_per_hour` learned per hour and **no** repeated full-body refetch storms in logs or proxy traffic. |
| 6 | **CR-014** Train-* leftover guard | After Train-* drains, confirm Train-* is empty and Trained-* has **no duplicates**. A `still holds N uid(s) already moved` warning means Exchange kept a source copy after MOVE. |
| 7 | **CR-006** scan `Delivered-To` | ByteLord's bare `bytelord` path is byte-identical. Spot-check `explain_score.py <acct> --uid <n>`: `BAYES_*` symbols should look the same as before. |
| 8 | **CR-003 / CR-007 / CR-008 / CR-009** rescue + retention guards | These only act in `flag`/`move` modes. Before promoting anyone, run **one test mailbox** in `move` mode and check: <br>• a spoof of an allowlisted vendor that Microsoft junked (`compauth=fail`) stays in Junk (`rescue_skipped m365_spoof_verdict`);<br>• old Archive mail dragged into Junk stays there (`rescue_skipped old_internaldate`);<br>• re-junking a rescued message produces `pending_spam` → `learn_spam`;<br>• allowlisted provider-Junk is never sent to Trash by retention in `flag` mode. |

### Decisions needed from the operator (not changed by the review)

1. **CR-004 (High) — Rspamd neural autotrain.** `neural.conf` autotrains from every `/checkv2` using Rspamd's *unadjusted* score. Bucket-B zeroing happens later in Python, so legitimate M365 mail (Amazon/Google at 18–25 internally) and the 2026-09-24 Trained-* re-score keep teaching neural "spam", and the pre-remediation `rn_*` keys were kept. Recommended: set `train { autotrain = false; }` (or `frozen = true;`), bump `unraid/bootstrap.version`, re-run bootstrap, and, **only with explicit approval**, delete the `rn_*` Redis keys (neural only).
2. **CR-014 (live check before `move` mode):** does Exchange leave the Inbox copy after the filter's `UID MOVE` Inbox→Junk (and Junk→Inbox for rescues)? If it does, spam stays visible in Inbox in move mode, so decide whether to detect it or expunge.
3. **CR-003 (Inbox side):** decided. Allowlisted spoof suspects stay in Inbox but are flagged (see above).
4. **CR-019:** HTTP `Rcpt` on scans is the first To/Cc address, not the mailbox as earlier docs claimed. Switching to the mailbox is more truthful but may add `FORGED_RECIPIENTS` points to list/BCC ham.
5. **CR-022/023 (compose):** `TZ` (Pacific) and `stop_grace_period: 90s` are now in `deploy/bytelord-compose.yaml`; **copy it to the live path** (below). `env_file` was deliberately left as is (low value, some risk). Optionally set `DASHBOARD_TRUSTED_PROXIES` to the Docker bridge gateway so login throttling sees real client IPs behind Caddy.

### Deploy when ready

The filter image and the ByteLord compose file changed. The live compose copy does **not** auto-sync, so diff and copy it first:

```bash
cd /opt/bytelord/projects/imap-spamfilter && git pull
diff -u /opt/bytelord/compose/imap-spamfilter/compose.yaml deploy/bytelord-compose.yaml
cp /opt/bytelord/compose/imap-spamfilter/compose.yaml \
   /opt/bytelord/compose/imap-spamfilter/compose.yaml.bak.$(date +%Y%m%d-%H%M%S)
cp deploy/bytelord-compose.yaml /opt/bytelord/compose/imap-spamfilter/compose.yaml
docker compose -f /opt/bytelord/compose/imap-spamfilter/compose.yaml config >/dev/null && echo OK
export SPAMFILTER_UID=1001 SPAMFILTER_GID=1001
docker compose -f /opt/bytelord/compose/imap-spamfilter/compose.yaml build spamfilter
docker compose -f /opt/bytelord/compose/imap-spamfilter/compose.yaml up -d --force-recreate --no-deps spamfilter
```

Leave `spamfilter-redis` and `spamfilter-rspamd` alone. Re-running `deploy/vps-bootstrap.sh` is optional (it only makes secret-config rendering umask-safe).

---

## Previously: where we left off (2026-09-24 evening)

IMAP-path false positives (buckets A+B+C) are **fixed and live**. Shared Bayes was **wiped and rebuilt**. Dashboard **Trained-*** rows were **rescored** so Messages shows current engine scores, not first-scan scores. Accounts remain **`mode: shadow`**.

Operator review of sample mail (Spambrella, Chase, BOA, Randy, Vancouver Bolt, Chelsea, iPic, 5Tool) showed:

- High stored scores with `BROKEN_HEADERS` / `HFILTER_*` were **old first-scan rows**. Rescored with current code they drop to ham territory when auth is clean.
- **Auth-passed content spam** (cold pitch / promo) can still score low: 5Tool −1.10, Chelsea ~0 after Bayes, iPic ~+6 from `MICROSOFT_SPAM`. Operator will Train-Spam those going forward.
- Chelsea was mistakenly in Trained-Ham; removed to Trained-Spam and Bayes flipped to spam (bobbi_rjmetalfab UID 15).

### Scoring stack (do not re-diagnose)

| Layer | Status |
|---|---|
| **A** | `HFILTER_HOSTNAME_UNKNOWN` / `RDNS_NONE` weight 0 in live `rspamd/local.d/hfilter_group.conf` |
| **B** | `apply_m365_auth_trust` suppresses DKIM/SPF/DMARC/`BLACKLIST_DMARC` only from outermost Microsoft AR (`mx.microsoft.com` **or** `compauth=` + `Received-SPF` receiver `protection.outlook.com`) |
| **C** | Bare `bayes_user` (`bytelord`) is **not** HTTP `Rcpt`; scan prepends `Delivered-To: bytelord` and uses the mailbox as `Rcpt`. Real broken MIME still scores `BROKEN_HEADERS` +8 |
| **Bayes** | Shared notebook `bytelord`; rebuilt 2026-09-24 (snapshot `redis/dump.rdb.bak-20260924`) |
| **Filter vs rspamd actions** | Filter uses `accounts.yml` `threshold`. Cosmetic rspamd lines in `actions.conf`: greylist 4, add_header 6, reject 15 |

### Dashboard vs live IMAP

Messages tab reads SQLite `our_score` / `score_detail`. Inbox and top-level Junk were **not** bulk-rescored (operator request). Trained-* **were** rescored into the DB (~2897 stored; ~8 oversize; some empty rspamd stubs repaired). Named Inbox senders vancouverbolt.com and randyhatley1@gmail.com were also updated so those rows show current scores.

---

## Bayes retrain (2026-09-24)

- Stopped `spamfilter` only; left rspamd/redis/unbound up. Never `compose down` redis.
- Deleted only `RSbytelord*` / `learned_ids` / occurrence keys; left fuzzy (`rgb*`/`rgm*`) and neural (`rn_*`).
- Trained-Spam rescored empty-Bayes: score **&lt; 6** → MOVE to Trained-Ham (**981** moved, **207** kept). Then `bootstrap_train.py --all-trained`.
- Redis after re-feed: **learns_spam≈205**, **learns_ham≈2410** (plus 20 Google + 20 Amazon clear-ham learns from newest Inbox mail).
- After restart checks: payroll 140596 **2.10**; Amazon 235843 **−2.05**; kept VSP spam **15.09**.

---

## Next steps (do these, in order)

1. **Pull `main`, sync compose, rebuild** the `spamfilter` image (see "Deploy when ready"), and work through the "Test these first" table above (Cursor can help).
2. **Decide CR-004 (neural autotrain)** before trusting scores for promotion; see "Decisions needed" above.
3. **Stay in shadow.** Keep teaching content spam via Train-Spam (5Tool-style cold pitch, Chelsea, iPic). Before `flag`/`move`, run one test mailbox in `move` mode to check the rescue/retention guards and the live Exchange MOVE semantics (CR-014).
4. **Do not** wipe Bayes again unless the operator asks.
5. Later: CR-016 supply chain; the Low items listed in `CLAUDE_OPUS5.5_EXTRA_CODE_FIXED.md` "Not fixed"; more mailboxes only when asked.
6. Optional later: bulk-rescore Inbox/Junk dashboard rows (not done; the operator excluded them).

---

## Project in one breath (verify details in IMPLEMENTATION_STATUS.md)

Self-hosted IMAP spam filter: Python (`filter/filter.py`) + Rspamd **4.2.0** + Redis Bayes + Unbound, Docker network **`spamnet`**. Sibling **`email-oauth2-proxy`** does XOAUTH2 to M365; this filter uses plain IMAP `LOGIN`. Shared Bayes user **`bytelord`**. **List hits are scored** then override routing. Allow drag → ham + Inbox; block drag → spam + Junk. Provider Junk is scored, **not** learned as spam; rescue only in `move` mode. Dashboard `https://spam.bytelord.net` (loopback **8099**); Rspamd WebUI link `https://spam.bytelord.net/rspamd/` when `RSPAMD_WEBUI_URL` is set.

---

## Paths and deploy gotcha

| Path | Role |
|---|---|
| `/opt/bytelord/projects/imap-spamfilter/` | Git checkout; `accounts.yml` gitignored |
| `deploy/bytelord-compose.yaml` | Compose **source of truth** |
| `/opt/bytelord/compose/imap-spamfilter/compose.yaml` | **Live** compose — **does not auto-sync**; diff + `cp` before pull/recreate |
| `/opt/bytelord/data/imap-spamfilter/state/` | SQLite `spamfilter.db` (0700/0600); retrain reports `bayes-retrain-20260924.tsv`, `google-amazon-20260924.tsv` |
| `/opt/bytelord/data/imap-spamfilter/redis/dump.rdb.bak-20260924` | Pre-wipe Redis snapshot |
| `/opt/bytelord/secrets/imap-spamfilter.env` | Secrets — never commit |
| `/opt/bytelord/projects/email-oauth2-proxy/` | OAuth/M365 bridge |

Recreate filter with `SPAMFILTER_UID=1001 SPAMFILTER_GID=1001`. **Do not** `compose down` redis (Bayes). Tests: Docker `python:3.12-slim` only.

---

## Key files

| File | Why |
|---|---|
| `IMPLEMENTATION_STATUS.md` | **Mandatory** durable map |
| `SESSION_HANDOFF.md` | This file |
| `CLAUDE_OPUS5.5_EXTRA_CODE_REVIEW.md` | 2026-09-25 review: 28 traced findings |
| `CLAUDE_OPUS5.5_EXTRA_CODE_FIXED.md` | What was fixed (15), what was deferred and why |
| `filter/test_opus_review_fixes.py` | Regression tests for those fixes |
| `filter/filter.py` | Scan/learn; `rspamd_scan_detail`; `apply_m365_auth_trust` |
| `filter/explain_score.py` | Re-score one UID and print symbols |
| `filter/bootstrap_train.py` | `--all-trained` Trained-* re-feed |
| `filter/dashboard.py` | Messages “why” from `score_detail` |
| `rspamd/local.d/` | `hfilter_group.conf`, `actions.conf` (cosmetic 4/6/15) |
| `README.md` | Operator docs (IMAP-path limitation section) |
| `design-arch/slice6_rspamd_scan_metadata.md` | Locked: no fake `Ip`/`Helo` |

---

## Agent protocol (short)

- Agent Mail project key: `/opt/bytelord/projects/imap-spamfilter`. Reserve files before edits; only the main session commits.
- Graphify before broad explore: `graphify explain` / `path`, or `graphify query "…" --dfs --budget 3333`. `graphify update .` after doc/code batches. Project `.cursor/rules/graphify.mdc` was **removed** (2026-09-23, commit `15cc5f8`); mandates live in `~/.claude/CLAUDE.md` and `~/.cursor/rules/`.
- Commit/push **only when asked**. No secrets, no force-push, no `--no-verify`.
