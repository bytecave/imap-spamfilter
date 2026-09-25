# Session handoff — imap-spamfilter (ByteLord VPS)

**Last updated:** 2026-09-25 late night (Pacific) — Train-* leftover fix live; third Bayes retrain done  
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
   - `dashboard Trained rescore score_detail`
   - `imap-spamfilter shadow dashboard 8099`
   Also `supermemory_list` (recent project memories). After decisions or live deploys, `supermemory_add` with `container=project`.

Also read `/home/bytecave/.claude/CLAUDE.md` (Cursor user rule) and use Agent Mail + graphify as that file and `IMPLEMENTATION_STATUS.md` § Agent onboarding require.

---

## Where we left off (2026-09-25 late night Pacific) — CONTINUE HERE

Cursor finished the Opus deploy verification, then shipped two live product changes and rebuilt Bayes from corrected Trained-* folders.

### Done tonight (after the Opus deploy)

1. **V1–V7 verification (read-only):** Pass except V3 Bayes `RS*` count was **60919** (not ≥78042). Notebook itself was intact (`rspamc stat` ~226 spam / ~2428 ham). Neural keys = 0. Fuzzy stock rule loaded. No NEURAL_* on sample scores.
2. **`rescue_below` (commit `ddca9c8`, deployed):** Provider Junk → Inbox only when the **first** score is **&lt; 4** (`accounts.yml` `rescue_below`, default 4). Inbox → Junk still uses `threshold` (default 8). Mid-band 4–8 stays put. User Inbox↔Junk drags are fingerprint-detected and never automoved back. Allow/block still override; spoofed allowlisted mail is not rescued from Junk. Rspamd `actions.conf` labels stay cosmetic.
3. **Train-* leftover cleanup (commit `0b982e9`, deployed):** Confirmed live: Exchange MOVE-as-COPY left dozens of copies in Train-* after learn→Trained-*. Drain now uses `_move_clearing_source` and `_clear_train_leftovers` (expunge only after a byte-identical copy is verified in Trained-*). Tests: **406 passed**.
4. **Third Bayes wipe + in-place relearn (retrain3):** Cause of earlier “spam in Trained-Ham”: morning one-shot `/tmp/rescore_trained_spam.py` had moved **981** Trained-Spam messages to Trained-Ham when Bayes was empty (score &lt; 6). Script deleted. Operator corrected folders by hand. Wipe: Redis bak `dump.rdb.bak-20260924-retrain3` inside the redis container; only `bytelord` Bayes keys deleted. After restart of rspamd, learns = 0, then `bootstrap_train.py --all-trained` **in place** (no score-based moves). Final Bayes: **spam ≈ 940**, **ham ≈ 1595**. Fuzzy hits in Trained-*: **3** (`FUZZY_DENIED`), all in `rich_rjmetalfab` Trained-Spam. Dashboard Trained-* rescored (rich_rjmetalfab spam folder needed a second pass with `spamfilter` stopped).
5. **Running image** matches `0b982e9` (`filter.py` hash equal host↔container). All accounts **`mode: shadow`**. Neural stays off.

### Next for a new agent

1. Human testing table below (esp. #1 list-drain, #6 Train-* leftover after a drag, #9 fuzzy FP watch). Stay in shadow.
2. Open operator decisions: CR-019 `Rcpt`; optional `DASHBOARD_TRUSTED_PROXIES`; CR-014 Inbox→Junk leftover check before promoting anyone to `move`.
3. Do **not** wipe Bayes again or run any score-based Trained-* mover unless the operator asks.
4. Do **not** re-run the Opus neural/fuzzy deploy runbook (kept below for reference only).

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
| `CLAUDE_OPUS5.5_EXTRA_CODE_REVIEW.md` | 2026-09-25 review: 29 traced findings |
| `CLAUDE_OPUS5.5_EXTRA_CODE_FIXED.md` | What was fixed (15 code + 2 rspamd config), what was deferred and why |
| `filter/test_opus_review_fixes.py` | Regression tests for those fixes |
| `filter/filter.py` | Scan/learn; `rspamd_scan_detail`; `apply_m365_auth_trust` |
| `filter/explain_score.py` | Re-score one UID and print symbols |
| `filter/bootstrap_train.py` | `--all-trained` Trained-* re-feed |
| `filter/dashboard.py` | Messages “why” from `score_detail` |
| `rspamd/local.d/` | `hfilter_group.conf`, `actions.conf` (cosmetic 4/6/15), `neural.conf` (autotrain off), `fuzzy_check.conf` (comments only → stock rule) |
| `README.md` | Operator docs (IMAP-path limitation section) |
| `design-arch/slice6_rspamd_scan_metadata.md` | Locked: no fake `Ip`/`Helo` |

---

## Agent protocol (short)

- Agent Mail project key: `/opt/bytelord/projects/imap-spamfilter`. Reserve files before edits; only the main session commits.
- Graphify before broad explore: `graphify explain` / `path`, or `graphify query "…" --dfs --budget 3333`. `graphify update .` after doc/code batches. Project `.cursor/rules/graphify.mdc` was **removed** (2026-09-23, commit `15cc5f8`); mandates live in `~/.claude/CLAUDE.md` and `~/.cursor/rules/`.
- Commit/push **only when asked**. No secrets, no force-push, no `--no-verify`.
