# Session handoff — imap-spamfilter (ByteLord VPS)

**Last updated:** 2026-09-29 ~21:30 Pacific. Next session: **Supermemory first** (investigate and fix, then capture the backlog), **then** the IMAP **connection errors** (`conn_error`). The operator set that order on 2026-09-29 evening. Everything below is live, committed and pushed.  
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
   - `Bayes retrain bytelord Delivered-To Rcpt rescue_below`
   - `Train leftover MOVE-as-COPY expunge`
   - `rich_bytecave move mode trained_retention_days Deleted Items`
   - `Bayes backup dump.rdb.bak-20260928-before-rich-move`
   - `URL_OBFUSCATED_TEXT word_dots Green Dot Bank`
   - `flag_untrained_junk Train-Ham inbox_copied ham_restored`
   - `kickstarlaunch.com domain block`
   - `imap-spamfilter shadow dashboard 8099`
   Also `supermemory_list` (recent project memories). After decisions or live deploys, `supermemory_add` with `container=project`.
   **Supermemory was unreachable for the whole 2026-09-28/29 Claude session (auth failed), so nothing from it was captured.** Add these once it is back:
   - `expire = 0` enabled rspamd Bayes expiry: ~374k tokens deleted; fixed and the notebook rebuilt 09-29.
   - Unbound now recurses (Spamhaus works).
   - `Pass: all` on `/checkv2`.
   - `flag_untrained_junk` must stay off (Outlook cached-mode conflicts).
   - `train_settle_seconds` 120.
   - Trained-* retention: 60 days from arrival.
   - The 1,186 messages rescued from Deleted Items.
   - The nightly 03:00 host backup stops all containers.

Also read `/home/bytecave/.claude/CLAUDE.md` (Cursor user rule) and use Agent Mail + graphify as that file and `IMPLEMENTATION_STATUS.md` § Agent onboarding require.

---

## CONTINUE HERE (2026-09-29 ~21:30 Pacific): Supermemory, then the connection errors

**Order (operator's decision, 2026-09-29 evening):**
1. **Supermemory:** investigate and fix, then add the backlog. Details in [§ Supermemory](#supermemory) below.
2. **IMAP connection errors:** everything known so far is in § "Connection errors: what is known" below.

Machine-wide follow-ups from the 2026-09-29 evening session are in
**`/opt/bytelord/scripts/BYTELORD_HANDOFF.md`**. They cover the Agent Mail edit gate (now live for Claude Code; Codex and Cursor still open), graphify's supervisor and 4k-chunk rebuild, and Supermemory's supervisor. That session changed nothing in this repo's code, and nothing was redeployed. Its only changes here: it removed graphify's git hooks (the supervisor refreshes the graph instead), deleted `.gitattributes` (it held only graphify's merge-driver line), and updated these two docs.

### Current live state (all committed, pushed and deployed)

- **Accounts:** 18 connected. `rich_bytecave` is `mode: move`; the other 17 are `shadow`. That includes 8 added 2026-09-29, among them `jamie.zinsli_rjmetalfab`, which connected at 03:48 after the operator's Exchange permission fix.
- **Filter image:** `imap-spamfilter:bytelord`, recreated 14:31 Pacific; the running code matches the repo.
- **Review:** the Claude Fable 5.1 review fixes and second pass are deployed ([`CLAUDE_FABLE5.1_CODE_FIXED.md`](CLAUDE_FABLE5.1_CODE_FIXED.md)).
- **Bayes:** rebuilt 2026-09-29, with 1,105 spam and 1,655 ham learns and about 213k tokens. rspamd's Bayes expiry is **off**; it used to delete tokens.
- **Unbound:** recurses on its own, so Spamhaus, SURBL and URIBL answer. rspamd scans send `Pass: all`.
- **Trained-* retention:** **60 days from arrival in the folder** (`trained_arrival` table). For rich_bytecave, nothing moves before 2026-11-28.
- **`flag_untrained_junk`: OFF, and it must stay off.** `accounts.yml` has a "DON'T ENABLE THIS" comment. Its flags made classic Outlook re-create moved mail in Junk.
- **Train-* settle:** `train_settle_seconds: 120`. Train-Spam/Train-Ham mail waits 2 minutes before it is learned and archived; the Train-Ham copy back to the Inbox is immediate.
- **IMAP audit log:** every mailbox-changing command the filter sends is logged as `imap_audit <cmd> in=<folder> <args>`. `IMAP_AUDIT=verbose` also logs SELECT/EXAMINE.
- **Open with the operator:**
  - Steve's classic Outlook may still bounce Train-Spam drags until its cache settles. The operator ran "Clear Offline Items" and removed the flags; re-test.
  - Duplicate copies of bounced messages may remain in Steve's Junk.

### Connection errors: what is known (starting point)

**Snapshot, `events` table, last 24 h, all 18 accounts (read-only query).**

| Count | Accounts | Detail |
|---|---|---|
| **270** | 18 | `conn_error "idle_done failed"`, about 10–24 per account per day |
| 10 | 7 | `Session invalidated - AccessTokenExpired` (on SEARCH) |
| 4 | 3 | `* BYE Session invalidated - AccessTokenExpired` (during IDLE) |
| 5 | 5 | `Broken pipe` |
| 1 | — | `LOGIN ***` |
| 1 | — | `Server Unavailable. 15` |
| 10 | 1 | jamie's `User is authenticated but not connected.` (all before the Exchange fix) |

The review's 7-day count was 1,561 "idle_done failed" (FABLE-CR-029).

**How the code gets there** (`filter/filter.py`):
- `wait_between_scans` IDLEs on the Inbox in chunks of at most 30 s (`wait = min(idle_timeout, max(30, junk_poll_interval))`), then calls `idle_done()`.
- If `idle_done` raises, it logs `idle_done failed: <reason>; forcing reconnect` and raises `IMAPClientError("idle_done failed")`.
- `_run_account` records that as `conn_error` and reconnects with backoff, which means a full login through `email-oauth2-proxy`.
- The **real reason is only in the log line**, not in the event row. The container log was reset by the 14:31 recreate, so collect fresh ones with:

  ```bash
  docker logs --since 2h spamfilter 2>&1 | grep -E "idle_done failed:|connection error:"
  ```

**Likely causes to check.**
1. **Token expiry.** Microsoft ends the IMAP session when the proxy's OAuth access token expires, about hourly (`AccessTokenExpired` BYE). A drop at IDLE end is then expected and recovers.
2. **An IDLE/DONE handling quirk** between imapclient, the proxy and Exchange.
3. **Proxy restarts:** the host's **03:00 nightly `bytelord-backup`** stops every container.

**Recommendation already on file (FABLE-CR-029).** Classify an expected session recycle (BYE/AccessTokenExpired/idle_done right after a long IDLE) as `conn_recycled`, so real errors stand out. No mail is lost today; the loop reconnects and resumes.

**Other items in this area:**
- `redact_log` replaces everything after the word "LOGIN" (OPUS-CR-024), which hides the reason in that one `LOGIN ***` error.
- Proxy logs: `docker logs email-oauth2-proxy`. That is a sibling project at `/opt/bytelord/projects/email-oauth2-proxy`; don't print its config, which holds secrets.

**Rules that still apply:**
- Do not restart during the 03:00 backup window.
- Do not `compose down` Redis.
- Tests: Docker `python:3.12-slim` with the **whole repository** mounted (**508 passed** last run; see IMPLEMENTATION_STATUS § Tests).

### Supermemory

**First job next session.** The operator reports Supermemory was working well until very early on 2026-09-29. Every Claude Code session since then started with the Supermemory plugin reporting "Authentication failed", and its MCP server (`plugin:supermemory:supermemory`) never connected, so nothing was recalled or captured. To do:

1. **Find out why Claude Code's Supermemory plugin stopped authenticating** and fix it. The hook message pointed at `https://console.supermemory.ai/auth/connect` or the `SUPERMEMORY_CC_API_KEY` environment variable. Check whether Cursor and Codex still reach it; the Codex hooks are in `~/.codex/hooks.json`. Never print or store the key.
2. **Check `supermemory-repo-supervisor.service`** (`/opt/bytelord/scripts/supermemory_repo_supervisor.sh`) for the flaws just fixed in the graphify supervisor:
   - retries every quiet period, forever, after a failure;
   - one `git` process per file per scan (the graphify one used 16 h of CPU in 19 h);
   - no alert when authentication fails.

   The fixes and the alert mechanism are described in `/opt/bytelord/scripts/BYTELORD_HANDOFF.md`.
3. **Capture the backlog** with `supermemory_add` (`container=project`):
   - the imap-spamfilter facts listed under "Mandatory before doing anything else" above;
   - the machine-wide 2026-09-29 decisions listed in `/opt/bytelord/scripts/BYTELORD_HANDOFF.md`.

## Earlier on 2026-09-29: review, deploy, Bayes rebuild (history)

**The full code and security review is done.** Claude Fable 5.1 reviewed `main` at `f53586a`:
- **32 findings**, traced requirement → test, in [`CLAUDE_FABLE5.1_CODE_REVIEW.md`](CLAUDE_FABLE5.1_CODE_REVIEW.md).
- **25 fixed in the tree**, each with regression tests, one commit per fix: [`CLAUDE_FABLE5.1_CODE_FIXED.md`](CLAUDE_FABLE5.1_CODE_FIXED.md).
- Full Docker suite: **487 passed**, up from 429. 47 of the new tests fail on the pre-review code.

**Deployed 2026-09-29 02:00–02:02 Pacific** (details below). `rich_bytecave` is still the only `mode: move` account, and nothing about modes, Bayes contents or live mail was changed.

### Deploy first: rspamd has been deleting Bayes tokens (FABLE-CR-001)

`expire = 0` in `rspamd/local.d/classifier-bayes.conf` does **not** mean "never expire". In rspamd 4.2.0 any number turns on the `bayes_expiry` module, and 0 makes it run `EXPIRE key 0` every minute on rare, one-class tokens, which deletes them.

From the live rspamd log (read-only):
- About **374,700 tokens deleted** since 2026-09-23, 361,000 of them on 2026-09-25, the retrain3 day.
- The notebook now has about 13,800 token keys.
- This is the unexplained 78,042 → 60,919 `RS*` drop noted on 2026-09-25.

**What the fix does and does not do.**
- The commit removes the line. Copy the file into the live `local.d` and restart `spamfilter-rspamd`; the commands are in `CLAUDE_FABLE5.1_CODE_FIXED.md` § "Deploying these fixes". Redis is not touched.
- Tokens already deleted do **not** come back.
- A plain `--all-trained` re-feed returns 208 ("already") from rspamd's learn cache and restores nothing.
- Rebuilding the notebook (backup, clear the `bytelord` notebook including its learn cache, re-feed Trained-*) is an **operator decision**.

### Train-* settle time: 2 minutes before learn/archive (live 2026-09-29 14:31 Pacific)

After `flag_untrained_junk` was turned off and the flags were cleared in Outlook, Train-Spam drags still bounced. Each drag added one `Sync Issues/Conflicts` entry. The filter was moving the message Train-Spam → Trained-Spam within about 30 s, before Outlook had finished uploading its own changes to the message it had just moved.

**New setting.** `train_settle_seconds` (default **120**): a message now waits that long in Train-Spam or Train-Ham before it is learned and moved. The timing uses the `trained_arrival` table. **The Train-Ham copy back to the Inbox is not delayed.**

**Verified live** on steve_rjmetalfab: a message dropped into Train-Spam waited about 2.5 minutes, then moved to Trained-Spam and stayed.

`accounts.yml` also now carries a "DON'T ENABLE THIS" comment on `flag_untrained_junk`.

### `flag_untrained_junk` turned OFF (2026-09-29 12:41 Pacific): it made Outlook bounce mail back to Junk

**Symptom.** In steve_rjmetalfab, mail dragged out of Junk (to the Inbox, or to Train-Spam) reappeared in Junk after a few seconds, often as 2–4 copies.

**Root cause, proven by tests and a new per-command IMAP audit log (`imap_audit` lines, commit `b2743be`):**
- The filter never sent a command moving anything to Junk.
- The copies are made by **Outlook in Cached Exchange Mode** resolving sync conflicts. Steve's `Sync Issues/Conflicts` folder has 59 entries for messages from 09-28/29, and they are the bounced ones.
- The trigger was the filter's `\Flagged` STORE on new untrained Junk (`flag_untrained_junk`, live since 09-28 19:22). Outlook turns the flag into a follow-up task, which is a local change it cannot reconcile once the item moves on the server. It then re-creates the item in Junk; the filter flagged each new copy, and the loop continued.

**The controlled test.** 3 never-flagged Junk messages moved to Trained-Spam stayed. 2 filter-flagged ones bounced and doubled. Removing the IMAP flag afterwards did **not** stop the bounce, because the conflict lives in that Outlook's local cache.

**Done.**
- `defaults.flag_untrained_junk: false` in the live `accounts.yml`, and the filter restarted. No new flags are set.
- The filter-set flags still on old Junk items were left alone. Removing them does not fix the bounce, and every server-side change on those items risks another conflict.

**Still needed, on the PC(s) running classic Outlook for steve@rjmetalfab.com.** Right-click **Junk Email → Properties → Clear Offline Items**, then let it re-sync. If it persists, rebuild that Outlook's OST. Only Steve's mailbox shows new conflicts.

Some duplicate copies of bounced messages, including a few made by the debugging tests, remain in Steve's Junk. The Leslie Harms "Purchase Order #3149955" is in his Inbox, learned as ham.

### Trained-* retention: 60 days from arrival (live 2026-09-29 10:39 Pacific)

`defaults.trained_retention_days: 60` is in the live `accounts.yml`. rich_bytecave's temporary `0` was removed. Trained-* ages now count from when the filter first saw the message in that folder (new `trained_arrival` table), not from delivery. Without that, 60 days would have swept ~311 of the just-rescued messages at once.

The clock started 2026-09-29 10:39 for 1,097 Trained-Ham and 118 Trained-Spam in rich_bytecave. The first possible sweep is 2026-11-28. The filter was rebuilt and recreated, and 18/18 accounts are connected.

### BAYES REBUILT 2026-09-29 02:29–03:48 Pacific (Claude, at the operator's request)

**Backups first:**
- SQLite: `state/spamfilter.db.bak-20260929-022922-before-retrain`
- Redis: `dump.rdb.bak-20260929-022922-before-retrain` and `appendonlydir.bak-20260929-022922-before-retrain` in `/data` of `spamfilter-redis`. AOF is on, so a restore needs both.

`spamfilter` was stopped for the whole job.

1. **Rescue** (rich_bytecave). **1,186** Trained-* messages moved from Deleted Items back where they came from: 1,085 to Trained-Ham, 101 to Trained-Spam.
   - Matching: 1,018 by body SHA-256, and 174 by a Message-ID that matched one copy or identical copies. 6 were already present.
   - 28 stayed in Deleted Items: their Message-ID matched several copies that were not identical.
   - The move record is `state/bayes-rescue-20260929-023312.json`.
2. **Wipe.** `RSbytelord`, `RSbytelord_*` and the `learned_ids` learn cache were deleted, then rspamd restarted. Fuzzy and every other key were left alone.
3. **Learn.** Every account's Trained-Spam and Trained-Ham were learned in place, plus the 48 earlier Inbox↔Junk and Allowlist/Blocklist drag learns (verified by SHA; 3 no longer exist).
   - Results: 2,760 learned, 75 already (same body in two mailboxes), 42 list-contradiction skips.
   - 173 were declined by rspamd for having fewer than 11 tokens. Those are short messages; that is expected.
   - **Bayes now: 1,105 spam and 1,655 ham learns, about 213,000 token keys** (13,797 before, when expiry was deleting them).
4. **Rescore.** All 3,000 live Trained-* messages were rescanned with the new Bayes, using the production scan parameters. `our_score` and `score_detail` were written directly, with no `scan` events. Same-body Train-*/Trained-* origin rows got the same score.
5. **Database tidy:**
   - Removed 1,190 rich_bytecave rows that still said "Deleted Items" for mail now back in Trained-*.
   - Removed 524 rows (487 of them rich_rjmetalfab) for mail no longer in any Trained-* folder, left over from the 09-24 shuffle. `inbox_copied` / `ham_restored` rows were not touched.

**Interruption.** The host's nightly `bytelord-backup.timer` (03:02, `server_backup create`) stops every container and restarts only those it stopped. It interrupted the rich_bytecave rescore, which was then finished after the backup. The backup therefore contains the new Bayes. **Don't schedule long maintenance across 03:00 Pacific.**

**Afterwards.** `spamfilter` was started, with no rebuild needed. **18/18 accounts connected**, including `jamie.zinsli_rjmetalfab` (the Exchange permission fix worked), which created its six folders. The dashboard's Messages → Trained Spam/Ham views show the new scores; `RECEIVED_SPAMHAUS_*` now appears on spam.

### DEPLOYED 2026-09-29 02:00–02:02 Pacific (Claude Fable 5.1, at the operator's request)

The whole bundle ran in order. Redis was never touched.

**Steps and results:**
1. Live compose synced from `deploy/bytelord-compose.yaml`. The backup is `compose.yaml.bak.20260929-*`, and the diff was exactly the three expected changes.
2. **Unbound** recreated. Spamhaus ZEN returns `127.0.0.10 .4 .2` and DBL returns `127.0.1.2` (real answers, no longer "refused"). The first lookups after a recreate can SERVFAIL for a few seconds while the cache warms; seen once, then clean.
3. **rspamd `local.d`** installed. The backup is `/opt/bytelord/data/imap-spamfilter/rspamd/local.d.bak.20260929-020048`, and every live file now matches the repo. One line was appended to the rendered `worker-controller.inc`. `configtest` gave syntax OK, then rspamd restarted.
   - `allow_file_and_shm_inputs = false` on 3 workers.
   - RBL rules: stock `spamhaus` plus `spamcop`.
   - No Bayes `expire`: since the restart the expiry module has run **no** steps. The last step logged was 09:00:49 UTC from the old process.
4. **Filter** rebuilt as `imap-spamfilter:bytelord` and recreated. It loaded 18 accounts, with no config errors and 0 restarts. `accounts.yml` now gives `m365_auth_trust: true` on every account and `trained_retention_days: 0` on `rich_bytecave`, both active.
   - The dashboard answers 200, and `SESSION_COOKIE_SECURE = True`.
   - `explain_score.py` on a live `rich_bytecave` message scans normally, with Bayes contributing and no `*_BLOCKED` symbol.

**Bayes baseline:** 13,797 `RS*_*` token keys at 02:07 Pacific. From now on it should only grow.

**New accounts:** 7 of the 8 added on 2026-09-29 connected, and each got all six folders, created and subscribed and confirmed by a read-only `LIST`/`LSUB`: aiden_eizenhoefer, matt, cad, foreman, shop, matta and mike (all `_rjmetalfab` except aiden).

**`jamie.zinsli_rjmetalfab` did not connect at first** *(fixed on the Exchange side, and connected at 03:48 the same day)*. Exchange answers LOGIN with `User is authenticated but not connected.`, so the proxy's OAuth token works but that mailbox cannot be opened. It has never connected, and no folders exist yet. The usual causes are on the Microsoft side:
- the app's service principal lacks `FullAccess` on that mailbox (`Add-MailboxPermission`);
- IMAP is disabled (`Get-CASMailbox … ImapEnabled`);
- the address in the proxy section or `accounts.yml` is not the mailbox's primary SMTP address, or the mailbox is unlicensed.

The filter retries with backoff up to 5 minutes. Once Exchange allows it, it connects and creates the folders by itself, with no restart needed.

### The deploy bundle (as run on 2026-09-29; kept for the next deploy)

The ordered, copy-paste steps are in [`CLAUDE_FABLE5.1_CODE_FIXED.md`](CLAUDE_FABLE5.1_CODE_FIXED.md) § "Deploying these fixes":
1. Sync the compose file (Unbound mount, Secure cookie, image `imap-spamfilter:bytelord`).
2. Recreate Unbound and check Spamhaus answers.
3. Copy the rspamd `local.d` files, append one line to the rendered `worker-controller.inc`, run configtest, restart rspamd.
4. Rebuild and recreate `spamfilter`.

Redis is never touched.

**Scores will move.**
- Spamhaus, SpamCop and the URI blocklists start adding points (FABLE-CR-005/032).
- Scans send `Pass: all`, so rspamd no longer stops at `reject = 15` (FABLE-CR-004).

The operator chose to deploy without a watch period.

### Bayes rescue from Deleted Items: feasibility confirmed (2026-09-29, read-only)

**Database backup** (before any rescue): `/opt/bytelord/data/imap-spamfilter/state/spamfilter.db.bak-20260929-013308-before-bayes-rescue`. Taken with SQLite's online backup, `integrity_check` ok, 10,454 message rows, mode 0600.

**Only `rich_bytecave` is affected.** It is the only move-mode mailbox, and retention is off in shadow. Retention moved **1,120 Trained-Ham + 103 Trained-Spam** to Deleted Items (events at 01:26, 02:07, 03:17 and 17:30 on 09-28). The database recorded, per moved message, the folder it came from:
- **915 ham + 103 spam** rows carry a body SHA-256;
- **202 ham** rows carry only a Message-ID;
- **3 ham** were moved without a database row, so they can't be identified.

Deleted Items holds 1,759 messages in all. A read-only sample (EXAMINE, nothing moved) matched every one it looked for:
- 12/12 ham and 8/8 spam with a SHA were **byte-identical**;
- 8/8 Message-ID-only ham were found; 6 matched exactly one message, 2 had duplicate copies.

**Rescue rules** (tool to be written with the operator):
- A Deleted Items message goes back to the Trained-* folder its row names only when its SHA matches.
- For the 202 Message-ID-only rows: only when exactly one Deleted Items message has that ID, or when all copies are byte-identical.
- Anything else stays in Deleted Items. That covers the ~536 messages retention did not put there.

**Before moving anything back, stop Trained-* retention on `rich_bytecave`.** Set `trained_retention_days: 0` on that account in `accounts.yml` and restart `spamfilter`. Retention goes by the original delivery date, so otherwise the hourly sweep moves the rescued mail straight back to Deleted Items.

The rebuild itself (clear the `bytelord` notebook **and** its learn cache, then `bootstrap_train.py --all-trained`) is still to be discussed.

### Test these first (human + Cursor), highest risk first

| # | Fix | What to check |
|---|---|---|
| 1 | **FABLE-CR-001** Bayes expiry off | After the rspamd restart, `docker logs --since 10m spamfilter-rspamd 2>&1 \| grep -c "finished expiry"` → **0**. Record the `RS*_*` key count (`redis-cli --scan --pattern 'RS*_*' \| wc -l`, with `REDISCLI_AUTH` as in V3), Train-Spam a few messages, and check a day later that the count only grew. |
| 2 | **FABLE-CR-004** `Pass: all` | For a day, watch `rich_bytecave` mail that scores near **8** (Inbox→Junk) and under **4** (provider-Junk rescue). `explain_score.py` on a message that used to score ≥ 15 should list more symbols (DMARC, fuzzy, RBL/URL rules) than its old `score_detail`. Report any legitimate mail newly over 8. |
| 3 | **FABLE-CR-009** Train-Ham of Junk mail | In `rich_bytecave`, drag a mis-junked message **from Junk** to Train-Ham. It must appear in the Inbox and **stay**. On the dashboard its Inbox row shows a score and `ham_restored`, with no `pending_ham` event. Repeat with a test sender you put on the User block list: in move mode that copy goes back to Junk. |
| 4 | **FABLE-CR-006 / 007** list editor | On User lists, save a typo (400), click the other Allow/Block option, press Cancel at the prompt, fix the typo and Save. The text must land in the list you had loaded. Open the same list in two tabs and Save in each: the second gets **409** "changed after you opened it" and writes nothing, and saving again overwrites on purpose. Also try a Save after an Outlook Blocklist drag landed while the page was open. |
| 5 | **FABLE-CR-008** oversize Train-Ham | Drop a message **over 5 MiB** into Train-Ham. A copy appears in the Inbox, and the Train-Ham message moves to Trained-Ham. |
| 6 | **FABLE-CR-002 / 003** hostile headers | Nothing to trigger live. `scan_failed` / `scan_giveup` stay at 0. An occasional `not safe to search` info line in `docker logs spamfilter` is expected and harmless. |
| 7 | **FABLE-CR-010** image name | After the deploy, `docker inspect -f '{{.Config.Image}}' spamfilter` → `imap-spamfilter:bytelord`. |
| 7a | **FABLE-CR-005 / 032** blocklists | After the deploy, new mail's `score_detail` / `explain_score.py` show no more `RBL_SPAMHAUS_BLOCKED_OPENRESOLVER` / `URIBL_BLOCKED`. Real listings appear as `RBL_SPAMHAUS_*`, `RECEIVED_SPAMHAUS_*`, `DBL_*`, `URIBL_*`, `SURBL_*` or `RBL_SPAMCOP`. Report legitimate mail pushed over 8 by one of them. |
| 7b | **FABLE-CR-030** Secure cookie | Log in at `https://spam.bytelord.net` and through the SSH tunnel at `http://127.0.0.1:8099`. Both must still work. |
| 7c | **FABLE-CR-032** file inputs | `docker exec spamfilter-rspamd rspamadm configdump worker \| grep -c "allow_file_and_shm_inputs = false"` → **3**. Scans and learns still work. |
| 8 | Tools | `bootstrap_train.py rich_bytecave NoSuchFolder spam --dry-run` exits **1**. `explain_score.py <acct> --message-id '<id>'` still explains a known message. |

### Operator decisions from this review (not changed in code)

- Rebuilding the Bayes notebook after the FABLE-CR-001 deploy (see above).
- FABLE-CR-005, 011 and 030, and the file-input and blocklist parts of 032, are **done** in the second pass (see the fix log).
- **Won't fix, by operator decision:**
  - `DASHBOARD_TRUSTED_PROXIES` (OPUS-CR-023);
  - a separate controller enable password;
  - a per-account healthcheck;
  - CI token scope;
  - image file ownership.
- **Standing rule: never change rspamd code.** It is replaced on every upstream update. `local.d` configuration is fine.
- **`m365_auth_trust`** defaults to true. Set `m365_auth_trust: false` on any future mailbox that Microsoft 365 does not deliver to.
- Still open from before: CR-014 and CR-019.

### Supermemory

This session could not reach Supermemory (authentication failed), so nothing was captured there. When it is available, add project memories for:
- `expire = 0` enables rspamd Bayes expiry; about 374k tokens deleted, 2026-09-23 → 09-29;
- `Pass: all` on `/checkv2`;
- Unbound forwards to Cloudflare, so RBLs are blocked;
- Train-Ham copies of Junk mail were handled as user reverts;
- the review/fix file names.

### After that

The Outlook add-in (`outlook-addin/`), built on the Windows desktop clone.

## Previously: where we left off (2026-09-28 19:22 Pacific)

`rich@bytecave.net` (`rich_bytecave`) is the only account in **`mode: move`**. The other nine stay **`shadow`**. `move_grace_seconds` is **0**. The `spamfilter` image was rebuilt from this tree at 19:22 Pacific and the container was recreated. All ten accounts reconnected. Full suite: **429 passed**.

### What landed this evening

1. **Untrained Junk follow-up flag.** `flag_untrained_junk: true` is in the live `accounts.yml` defaults (that file is gitignored). Builtin default remains false. Every account, including shadow, flags Junk that is **new above the Junk bookmark** and has not been taught (`learned_as` or `pending_learn` of spam/ham, on that row or a same-body sibling). Filter-owned Junk and provider Junk that stays are flagged. A user Inbox→Junk drag is not, because that drag is the spam teach. On `rich_bytecave`, provider Junk that is about to be rescued (score under 4, or allow) is not flagged. Mail already in Junk when the setting was turned on was not backfilled. Event: `junk_untrained_flagged`. Exchange exposes only `\Flagged` (the red follow-up flag).
2. **Train-Ham returns a copy to the Inbox.** On seeing a message in Train-Ham, the filter `COPY`s it to the Inbox before `try_learn`, stores the unchanged body's SHA-256 (`our_action=inbox_copied`), and when UIDPLUS returns the new UID marks that Inbox row `ham_restored`. *(Correction 2026-09-29: that UIDPLUS pre-mark never ran, because imapclient returns no COPYUID for UID COPY. The hold works by body fingerprint in `scan_inbox`; see FABLE-CR-009.)* The learn then still MOVEs the Train-Ham message to Trained-Ham. The Inbox copy stays even when the score is 8 or higher. A block-list hit still sends it to Junk. A byte-identical copy already in the Inbox is not copied a second time. The copy runs even if the hourly learn budget is spent. A failed copy leaves the message in Train-Ham and skips the learn that pass. Older ham (including the accidental Trained-Ham reversal) does **not** hold a new Inbox copy, because those rows have no `inbox_copied` mark. Train-Spam does not copy to the Inbox.
3. **`@kickstarlaunch.com` domain block** is live in SQLite for `bytecave.net`, `bytelord.net`, `eizenhoefer.net`, and `rjmetalfab.com`. It is not in git. New mail is routed by the list (move mode to Junk, shadow logs only) and is not Bayes-learned from the list hit alone. Copies already scored before the insert were left where they were.

### Still in force from this morning

`@host` list entries match that host and its subdomains (`8d310b9`, in the running image). `url_suspect` `word_dots = false` is committed and live. Neural stays off. Do not wipe Bayes. Do not promote the other nine accounts.

### Next

1. ~~Another agent does a full code and security review and fixes what it confirms.~~ **Done 2026-09-29** (Claude Fable 5.1). See the continue-here section above.
2. The **Outlook add-in is still planned.** `outlook-addin/` holds the requirements and setup notes (`d988d40`). Building it waits until after the review.
3. CR-014 (Inbox→Junk MOVE-as-COPY leftover) is still open. CR-019 `Rcpt` and `DASHBOARD_TRUSTED_PROXIES` are still open.

### Retention is on for the move-mode mailbox

The 01:34 note below is the first sweep. Later hourly sweeps of up to 500 may have continued. Do not set `trained_retention_days` unless the operator asks.

### Urgent: retention is on for this mailbox (01:34 Pacific)

Shadow skips retention. Move mode does not. Default `trained_retention_days` is **7** (not set in `accounts.yml`). The first sweep after the mode change, at 01:26 Pacific, moved mail older than about 8 days to **Deleted Items**:

- **101** from `Junk Email/Trained-Spam`
- **500** from `Junk Email/Trained-Ham` (per-pass cap is 500, so more old ham remains)

The next sweep is about **one hour** after that (`retention_check_interval` default 3600). Junk retention is 62 days and did not move anything on that pass. Bayes itself was not wiped. To stop further Trained-* moves, set `trained_retention_days: 0` on `rich_bytecave` (or a large number) and restart `spamfilter`. Do not do that unless the operator asks. Messages are in Deleted Items, not destroyed.

### Bayes backup (before the mode change)

Taken with `spamfilter` stopped, after a successful `BGSAVE`. Redis `requirepass` is quoted; a raw `sed` of that line fails `AUTH`. Inside the redis container:

- `/data/dump.rdb.bak-20260928-before-rich-move` — 1,446,079 bytes, byte-identical to `dump.rdb` at 08:25 UTC
- `/data/appendonlydir.bak-20260928-before-rich-move` — AOF is enabled, so a restore must stop Redis and put **both** back

Host path of that volume: `/opt/bytelord/data/imap-spamfilter/redis/`. Do not `compose down` Redis. Do not wipe Bayes or run a score-based Trained-* mover unless asked.

### What move mode does, and does not, teach

Score moves do **not** train Bayes. Provider Junk with score **&lt; 4** is rescued to Inbox with no ham learn. Inbox score **≥ 8** goes to Junk with no spam learn. Mid-band 4–8 stays put. A drag Junk→Inbox schedules ham, and Inbox→Junk schedules spam, after `learn_grace_seconds` **30**. Undo before that drops the pending learn. The Inbox bookmark does **not** go back and re-junk mail already scored in shadow. New UIDs above the bookmark, plus up to 50 never-scored Inbox rows per pass, are acted on.

### Morning items that are now committed and live

| Change | Live? |
|---|---|
| `url_suspect` `word_dots = false` | **Yes.** Stops “Green Dot Bank” → `URL_OBFUSCATED_TEXT` +9. Other obfuscation patterns stay on. |
| `@host` allow/block matches that host **and subdomains**; longer host wins inside a rank | **Yes.** In `8d310b9` and in the image rebuilt 2026-09-28 evening. |
| Train-Ham Inbox copy and `flag_untrained_junk` | **Yes.** See the continue-here section above. |

Do not rebuild `spamfilter` unless asked.

### Also true from 2026-09-25 (still in force)

Rescue line is `rescue_below` 4; junk line is `threshold` 8. Train-* drain expunges MOVE-as-COPY leftovers after a verified Trained-* copy (`0b982e9`). Retrain3 Bayes was about **940 spam / 1595 ham**. Neural stays off. Fuzzy is the stock rspamd.com rule. Dashboard Class=spam means a filter spam **action** or `learned_as=spam`, not “sitting in Junk.” A Train-Ham restore is `ham_restored`, which is not in that spam-action set.

The numbered “next for a new agent” that used to sit here is replaced by the continue-here section at the top of this file. Do not re-run the Opus neural/fuzzy deploy runbook (kept below for reference only). V1–V7 is done. The V1 snippet that expects a clean tree and `1d04635` at the top is historical; it is not the current check.

## Cursor: first job — verify the 2026-09-25 deploy (read-only) — DONE 2026-09-24 night

The operator already ran the deploy runbook (below). Your job is to **confirm independently** that each step took effect, then report a pass/fail table to the operator.

**Rules for this section:**
- **Read-only.** Do not stop, restart, or recreate containers. Do not edit live config, touch Redis keys, or change `accounts.yml`. Do **not** re-run the runbook.
- **If a check fails,** stop, show the operator the exact output, and propose a fix. Do not apply it without their approval.
- **Never print secrets.** Do not `cat` the secrets file, print the container's full environment, or echo `REDISCLI_AUTH`.
- Run everything from `/opt/bytelord/projects/imap-spamfilter` as `bytecave`.

**V1: code (runbook step 1)**
```bash
cd /opt/bytelord/projects/imap-spamfilter
git status --short                                     # Expect: no output
git fetch -q origin main && git status -sb | head -1   # Expect: "## main...origin/main" with no [behind N]
git log --oneline -5                                   # Expect: 1d04635 "Use rspamd's stock fuzzy rule ..." at the top, or below docs-only commits
```

**V2: live rspamd config (steps 2, 4, and 4b)**
```bash
LIVE=/opt/bytelord/data/imap-spamfilter/rspamd/local.d
for f in rspamd/local.d/*; do n=$(basename "$f"); case "$n" in *.template) continue;; esac; cmp -s "$f" "$LIVE/$n" || echo "DIFFERS: $n"; done
# Expect: no output (every live file matches the repo)
docker exec spamfilter-rspamd rspamadm configtest
# Expect: "syntax OK", and no "bad encryption key value" line
docker exec spamfilter-rspamd rspamadm configdump neural | grep -i autotrain
# Expect: autotrain = false;
docker exec spamfilter-rspamd rspamadm configdump fuzzy_check | grep -E "encryption_key|servers"
# Expect: key icy63itbhhni8bq15ntp5n5symuixf73s1kpjh6skaq4e7nx5fiy and servers service=fuzzy+rspamd.com
docker logs --since 24h spamfilter-rspamd 2>&1 | grep -i fuzzy | tail -20
# Expect: no repeated timeout/"cannot" errors. If there are, outbound UDP 11335 to rspamd.com is probably
# blocked: fuzzy then silently scores nothing. That is not a false-positive risk, but tell the operator.
```

**V3: Redis backup and neural keys (steps 3 and 4)**
```bash
ls -la /home/bytecave/spamfilter-redis-before-neural-wipe-*.rdb
# Expect: at least one file, mode -rw-------, several MB
export REDISCLI_AUTH="$(sed -nE 's/^(export[[:space:]]+)?REDIS_PASSWORD=//p' /opt/bytelord/secrets/imap-spamfilter.env | tail -n1 | tr -d "\"'\r")"
docker exec -e REDISCLI_AUTH spamfilter-redis sh -c 'redis-cli --scan --pattern "rn_*"; redis-cli --scan --pattern "rn[0-9]*"' | wc -l
# Expect: 0. With autotrain off, rspamd 4.2.0 writes no neural keys; a non-zero count means something is training neural.
docker exec -e REDISCLI_AUTH spamfilter-redis sh -c 'redis-cli --scan --pattern "RS*" | wc -l'
# Expect: 78042 or more (it grows as the operator trains). A large DROP means Bayes data was lost: stop and report.
unset REDISCLI_AUTH
```

**V4: compose file, time zone, and shutdown grace (step 5)**
```bash
cmp deploy/bytelord-compose.yaml /opt/bytelord/compose/imap-spamfilter/compose.yaml && echo SAME   # Expect: SAME
ls -la /opt/bytelord/compose/imap-spamfilter/compose.yaml.bak.*       # Expect: the pre-deploy backup exists
docker inspect -f '{{.Config.StopTimeout}}' spamfilter                # Expect: 90
docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' spamfilter | grep '^TZ='
# Expect: TZ=America/Los_Angeles  (only ever grep this output; the full environment contains secrets)
date; docker exec spamfilter date                                     # Expect: both in Pacific time (PDT/PST)
```

**V5: the new code is what's running (step 6)**
```bash
sha256sum filter/filter.py filter/dashboard.py
docker exec spamfilter sha256sum /app/filter.py /app/dashboard.py
# Expect: the same two hashes. A mismatch means the image is older than the checkout, so ask the operator before rebuilding.
docker exec spamfilter grep -c "allowlisted_spoof_suspect" /app/filter.py   # Expect: a number greater than 0
```

**V6: health (step 7)**
```bash
docker compose -f /opt/bytelord/compose/imap-spamfilter/compose.yaml ps
# Expect: all four containers Up; spamfilter shows (healthy)
docker logs spamfilter 2>&1 | grep -c "connected, delimiter"
# Expect: 10 or more (one per account per connect)
docker logs --since 24h spamfilter 2>&1 | grep -iE "unhandled|database is locked|Traceback|giving up on" | tail -20
# Expect: nothing, or something you can explain to the operator
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8099/login   # Expect: 200
docker exec -i spamfilter python - <<'EOF'
import sqlite3, time
c = sqlite3.connect("file:/state/spamfilter.db?mode=ro", uri=True)
for ev, n in c.execute("SELECT event, COUNT(*) FROM events WHERE ts > ? GROUP BY event ORDER BY 2 DESC", (int(time.time()) - 86400,)):
    print(f"{n:6d}  {ev}")
EOF
# Expect: scan-type events dominate. scan_giveup should be absent (see test #2 if present).
# allowlisted_spoof_suspect / no_message_id may appear occasionally; list those to the operator.
```

**V7: scores (neural gone, fuzzy present)**
```bash
docker exec spamfilter python explain_score.py <account> --uid <recent Inbox UID>
# <account> is a name from accounts.yml; take a UID from the dashboard Messages page.
# Expect: no NEURAL_SPAM / NEURAL_HAM lines. FUZZY_* lines are new and may appear on some mail.
```
Repeat V7 on 3–5 recent messages, including one obvious spam and one normal message.

**Then:**
- Report the V1–V7 results to the operator as a table.
- `supermemory_add` (container=project): "2026-09-25 Opus review deployed and verified", with the facts above.
- Move on to the reading list and the test table.

### Reading list for Cursor (what changed, what didn't)

1. `CLAUDE_OPUS5.5_EXTRA_CODE_FIXED.md`, section **Fixes**: every change, with the tests that cover it.
2. `CLAUDE_OPUS5.5_EXTRA_CODE_FIXED.md`, section **Not fixed — recommendations for the operator**: what was deliberately left alone, and why.
3. `CLAUDE_OPUS5.5_EXTRA_CODE_REVIEW.md`: the full evidence for any finding you need to understand.
4. "Decisions" below, and IMPLEMENTATION_STATUS § "Current product policy" (including "Neural: why it stays off").

## After verification: testing, decisions, and the deploy record

### Test these first (highest risk / most behavior change)

| # | Fix | What to check on the live system |
|---|---|---|
| 1 | **CR-001** list-drain de-dup (`_identical_copies_in_folder`) | Drag a message from Junk → `INBOX/Allowlist`, and one from Inbox → `INBOX/Blocklist`. Each must land in Inbox/Junk respectively and leave no copy in the list folder. The Exchange leftover cleanup should still log "byte-identical copy ... expunging leftover copies". A duplicate in the destination (instead of a deletion) is the new safe failure mode. |
| 2 | **CR-002** poison give-up (`scan_giveup`) | Stop `spamfilter-rspamd` for more than 10 minutes while mail arrives, then restart it. Expect `scan_failed` → halt → resume, and **no** `scan_giveup` events. Any `giving up on ...` log line or `scan_giveup` event on the Events page deserves a look with `explain_score.py`. |
| 3 | **CR-005** dashboard list Save > 16 KiB | Paste about 700 test addresses into a *test* user list and Save. It must succeed (it used to return 413), then Cancel/remove them. `/login` must still reject huge bodies. |
| 4 | **CR-011** `BEGIN IMMEDIATE` | Watch `docker logs spamfilter` for `database is locked` or `unhandled error in account loop` (should drop), and make sure no worker stalls while the dashboard saves lists. |
| 5 | **CR-013** learn budget before fetch | Drop about 100 messages into one account's Train-Spam. Expect roughly `max_learns_per_hour` learned per hour and **no** repeated full-body refetch storms in logs or proxy traffic. |
| 6 | **CR-014** Train-* leftover cleanup | Confirmed live 2026-09-24: Exchange kept Train-* copies after MOVE. The drain now expunges the leftover right after MOVE, and on later passes expunges an old leftover once a byte-identical copy is verified in Trained-* (`train_leftover_expunged` event). After a drag, confirm Train-* empties and Trained-* has **no duplicates**. A `still holds N uid(s) already moved` warning now means a leftover with no verified copy was kept. |
| 7 | **CR-006** scan `Delivered-To` | ByteLord's bare `bytelord` path is byte-identical. Spot-check `explain_score.py <acct> --uid <n>`: `BAYES_*` symbols should look the same as before. |
| 8 | **CR-003 / CR-007 / CR-008 / CR-009** rescue + retention guards | These only act in `flag`/`move` modes. Before promoting anyone, run **one test mailbox** in `move` mode and check: <br>• a spoof of an allowlisted vendor that Microsoft junked (`compauth=fail`) stays in Junk (`rescue_skipped m365_spoof_verdict`);<br>• old Archive mail dragged into Junk stays there (`rescue_skipped old_internaldate`);<br>• re-junking a rescued message produces `pending_spam` → `learn_spam`;<br>• allowlisted provider-Junk is never sent to Trash by retention in `flag` mode. |
| 9 | **CR-029** fuzzy now scores | Over the next few days, `FUZZY_DENIED` (up to +12) or `FUZZY_PROB` (up to +5) should show up on some spam in `explain_score.py` or the dashboard's "why" column, and `FUZZY_WHITE` (−2.1) on some bulk ham. **Any `FUZZY_DENIED` on mail the operator considers legitimate:** report it with the UID. It is a false-positive risk that did not exist before. |
| 10 | **CR-004** neural off | No `NEURAL_SPAM`/`NEURAL_HAM` in any new score; V3's neural key count stays 0. |
| 11 | Allowlisted spoof flag (shadow) | Look for `allowlisted_spoof_suspect` events or `[shadow] would flag allowlisted spoof suspect` log lines. For each one, check the headers: is it really a spoof of an allowlisted sender? A legitimate allowlisted sender triggering this is a false alarm to report. |
| 12 | `TZ` Pacific | Dashboard times and per-day buckets match the operator's local clock. |

### Decisions needed from the operator (not changed by the review)

1. **CR-004 (High) — Rspamd neural autotrain: done and deployed 2026-09-25.** `autotrain = false`; neural keys deleted after a Redis backup. Neural adds no score. It **stays off by decision**: see IMPLEMENTATION_STATUS § "Neural: why it stays off" before proposing to turn it back on.
2. **CR-014 (live check before `move` mode):** does Exchange leave the Inbox copy after the filter's `UID MOVE` Inbox→Junk (and Junk→Inbox for rescues)? If it does, spam stays visible in Inbox in move mode, so decide whether to detect it or expunge.
3. **CR-003 (Inbox side):** decided. Allowlisted spoof suspects stay in Inbox but are flagged (see above).
4. **CR-019:** HTTP `Rcpt` on scans is the first To/Cc address, not the mailbox as earlier docs claimed. Switching to the mailbox is more truthful but may add `FORGED_RECIPIENTS` points to list/BCC ham.
5. **CR-022/023 (compose):** `TZ` (Pacific) and `stop_grace_period: 90s` are deployed (live compose = repo, verified by V4). `env_file` was deliberately left as is (low value, some risk). Optionally set `DASHBOARD_TRUSTED_PROXIES` to the Docker bridge gateway so login throttling sees real client IPs behind Caddy.
6. **CR-029 — fuzzy: fixed and deployed 2026-09-25.** Stock `rspamd.com` rule; watch for `FUZZY_DENIED` on legitimate mail (test #9).

### Deploy runbook — DONE by the operator 2026-09-25 (kept for reference; do not re-run)

**Already executed.** Cursor verifies it with V1–V7 above and must not re-run it (steps 3–4 stop services and delete Redis keys). The runbook is kept as the pattern for future deploys.

Run each step, look at the output, and only continue if it matches "Expect". If anything looks different, stop and paste the output to your assistant. The filter and dashboard are down only between steps 4 and 6 (a few minutes).

**Step 1 — pull the code**

```bash
cd /opt/bytelord/projects/imap-spamfilter
git status --short        # Expect: no output. If files are listed, STOP.
git pull
git log --oneline -1      # Expect: "Turn off rspamd neural self-training ..." (or newer)
```

**Step 2 — install the new neural and fuzzy configs into the live rspamd config folder**

```bash
LIVE=/opt/bytelord/data/imap-spamfilter/rspamd/local.d
for f in rspamd/local.d/*; do n=$(basename "$f"); case "$n" in *.template) continue;; esac; cmp -s "$f" "$LIVE/$n" || echo "DIFFERS: $n"; done
# Expect exactly two lines: DIFFERS: fuzzy_check.conf and DIFFERS: neural.conf   (anything else: STOP)
cp rspamd/local.d/neural.conf "$LIVE/neural.conf"
cp rspamd/local.d/fuzzy_check.conf "$LIVE/fuzzy_check.conf"
grep autotrain "$LIVE/neural.conf"     # Expect: autotrain = false;
```

(2026-09-25: the operator ran steps 1–4 before the fuzzy fix existed; the fuzzy file was then installed as "step 4b", see OPUS-CR-029 in `CLAUDE_OPUS5.5_EXTRA_CODE_FIXED.md`.)

**Step 3 — back up Redis and look at the neural keys (read-only)**

```bash
export REDISCLI_AUTH="$(sed -nE 's/^(export[[:space:]]+)?REDIS_PASSWORD=//p' /opt/bytelord/secrets/imap-spamfilter.env | tail -n1 | tr -d "\"'\r")"
docker exec -e REDISCLI_AUTH spamfilter-redis redis-cli SAVE          # Expect: OK
docker cp spamfilter-redis:/data/dump.rdb ~/spamfilter-redis-before-neural-wipe-$(date +%Y%m%d-%H%M%S).rdb
chmod 600 ~/spamfilter-redis-before-neural-wipe-*.rdb && ls -la ~/spamfilter-redis-before-neural-wipe-*.rdb
docker exec -e REDISCLI_AUTH spamfilter-redis sh -c 'redis-cli --scan --pattern "RS*" | wc -l'   # Bayes key count: write it down
docker exec -e REDISCLI_AUTH spamfilter-redis sh -c 'redis-cli --scan --pattern "rn_*"; redis-cli --scan --pattern "rn[0-9]*"' | head -20
# Expect: only names starting with rn_ or rn3_ (for example rn_default_..., rn3_default_...)
```

**Step 4 — stop the filter and rspamd, delete only the neural keys, start rspamd**

```bash
docker stop -t 90 spamfilter
docker stop spamfilter-rspamd
docker exec -e REDISCLI_AUTH spamfilter-redis sh -c 'redis-cli --scan --pattern "rn_*" | xargs -r -n 100 redis-cli UNLINK; redis-cli --scan --pattern "rn[0-9]*" | xargs -r -n 100 redis-cli UNLINK'
docker exec -e REDISCLI_AUTH spamfilter-redis sh -c 'redis-cli --scan --pattern "rn_*"; redis-cli --scan --pattern "rn[0-9]*"' | wc -l   # Expect: 0
docker exec -e REDISCLI_AUTH spamfilter-redis sh -c 'redis-cli --scan --pattern "RS*" | wc -l'   # Expect: same Bayes count as step 3
unset REDISCLI_AUTH
docker start spamfilter-rspamd
sleep 10
docker exec spamfilter-rspamd rspamadm configdump neural | grep -i autotrain   # Expect: autotrain = false;
docker exec spamfilter-rspamd rspamadm configtest   # Expect: syntax OK, and no "bad encryption key value" line
docker exec spamfilter-rspamd rspamadm configdump fuzzy_check | grep -E "encryption_key|servers"
# Expect: encryption_key = "icy63itbhhni8bq15ntp5n5symuixf73s1kpjh6skaq4e7nx5fiy"; and servers = "service=fuzzy+rspamd.com";
```

**Step 5 — install the new compose file (Pacific time, 90 s shutdown grace)**

```bash
diff -u /opt/bytelord/compose/imap-spamfilter/compose.yaml deploy/bytelord-compose.yaml
# Expect: only the TZ line (Europe/Berlin -> America/Los_Angeles, plus a comment)
# and a new stop_grace_period: 90s block under the spamfilter service.
cp /opt/bytelord/compose/imap-spamfilter/compose.yaml \
   /opt/bytelord/compose/imap-spamfilter/compose.yaml.bak.$(date +%Y%m%d-%H%M%S)
cp deploy/bytelord-compose.yaml /opt/bytelord/compose/imap-spamfilter/compose.yaml
docker compose -f /opt/bytelord/compose/imap-spamfilter/compose.yaml config >/dev/null && echo OK   # Expect: OK
```

**Step 6 — rebuild and start the filter with the new code**

```bash
export SPAMFILTER_UID=1001 SPAMFILTER_GID=1001
docker compose -f /opt/bytelord/compose/imap-spamfilter/compose.yaml build spamfilter
docker compose -f /opt/bytelord/compose/imap-spamfilter/compose.yaml up -d --force-recreate --no-deps spamfilter
```

**Step 7 — check it came back**

```bash
docker compose -f /opt/bytelord/compose/imap-spamfilter/compose.yaml ps
# Expect: spamfilter and spamfilter-rspamd "Up" (spamfilter turns "healthy" within ~2 min)
docker logs --since 5m spamfilter 2>&1 | grep -c "connected, delimiter"   # Expect: 10 (one per account)
docker logs --since 5m spamfilter 2>&1 | grep -iE "error|unhandled|database is locked" | head
# Expect: nothing alarming (a few "scan failed" lines right at startup are OK)
date; docker exec spamfilter date   # Expect: both show Pacific time (PDT/PST)
```

Later, `docker exec spamfilter python explain_score.py rich_bytecave --uid <n>` on new mail should show no `NEURAL_SPAM`/`NEURAL_HAM` lines.

**If something goes wrong:** keep the `~/spamfilter-redis-before-neural-wipe-*.rdb` backup and the `compose.yaml.bak.*` file, and ask for help before changing anything else. Redis runs with AOF enabled, so restoring from the RDB file is a deliberate procedure, not a copy.

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

1. **Cursor: run V1–V7** ("Cursor: first job" above) and report to the operator. The deploy itself is done.
2. **Work through "Test these first"** with the operator (rows 1–7 and 9–12 in shadow; row 8 needs one test mailbox in `move` mode).
3. Watch scores for a few days: `NEURAL_*` is gone and `FUZZY_*` is new. Report any legitimate mail whose score jumped because of `FUZZY_DENIED`.
4. **Stay in shadow.** Keep teaching content spam via Train-Spam (5Tool-style cold pitch, Chelsea, iPic). Before `flag`/`move`, run one test mailbox in `move` mode to check the rescue/retention guards and the live Exchange MOVE semantics (CR-014).
5. **Do not** wipe Bayes again unless the operator asks. **Do not** turn neural back on (see IMPLEMENTATION_STATUS).
6. Later: CR-016 supply chain; the Low items listed in `CLAUDE_OPUS5.5_EXTRA_CODE_FIXED.md` "Not fixed"; more mailboxes only when asked.
7. Optional later: bulk-rescore Inbox/Junk dashboard rows (not done; the operator excluded them).

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
| `/opt/bytelord/data/imap-spamfilter/redis/dump.rdb.bak-20260928-before-rich-move` | Bayes snapshot before `rich_bytecave` move mode (pair with `appendonlydir.bak-20260928-before-rich-move`) |
| `/opt/bytelord/data/imap-spamfilter/redis/dump.rdb.bak-20260924-retrain3` | Snapshot before the third Bayes relearn |
| `/opt/bytelord/secrets/imap-spamfilter.env` | Secrets — never commit |
| `/opt/bytelord/projects/email-oauth2-proxy/` | OAuth/M365 bridge |

Recreate filter with `SPAMFILTER_UID=1001 SPAMFILTER_GID=1001`. **Do not** `compose down` redis (Bayes). Tests: Docker `python:3.12-slim` only.

---

## Key files

| File | Why |
|---|---|
| `IMPLEMENTATION_STATUS.md` | **Mandatory** durable map |
| `SESSION_HANDOFF.md` | This file |
| `CLAUDE_OPUS5.5_EXTRA_CODE_REVIEW.md` | 2026-09-25 review: 29 traced findings |
| `CLAUDE_OPUS5.5_EXTRA_CODE_FIXED.md` | What was fixed (15 code + 2 rspamd config), what was deferred and why |
| `filter/test_opus_review_fixes.py` | Regression tests for those fixes |
| `filter/filter.py` | Scan/learn; `rspamd_scan_detail`; `apply_m365_auth_trust` |
| `filter/explain_score.py` | Re-score one UID and print symbols |
| `filter/bootstrap_train.py` | `--all-trained` Trained-* re-feed |
| `filter/dashboard.py` | Messages “why” from `score_detail` |
| `rspamd/local.d/` | `hfilter_group.conf`, `actions.conf` (cosmetic), `neural.conf` (autotrain off), `fuzzy_check.conf` (comments only), `url_suspect.conf` (word-dot check off; uncommitted, live copy installed) |
| `README.md` | Operator docs (IMAP-path limitation section) |
| `design-arch/slice6_rspamd_scan_metadata.md` | Locked: no fake `Ip`/`Helo` |

---

## Agent protocol (short)

- Agent Mail project key: `/opt/bytelord/projects/imap-spamfilter`. Reserve files before edits; only the main session commits.
- Graphify before broad explore: `graphify explain` / `path`, or `graphify query "…" --dfs --budget 3333`. `graphify update .` after doc/code batches. Project `.cursor/rules/graphify.mdc` was **removed** (2026-09-23, commit `15cc5f8`); mandates live in `~/.claude/CLAUDE.md` and `~/.cursor/rules/`.
- Commit/push **only when asked**. No secrets, no force-push, no `--no-verify`.
