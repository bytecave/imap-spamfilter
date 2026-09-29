# Implementation status — imap-spamfilter (ByteLord)

**Last updated:** 2026-09-29 Pacific. The Claude Fable 5.1 code and security review is **done**: 32 findings, 25 fixed in the tree, **none deployed yet**. Deploy the Bayes-expiry fix (FABLE-CR-001) first. Then the planned Outlook add-in.  
**Audience:** brand-new agent sessions (Cursor / Claude Code / Codex) with no prior chat memory.  
**Companion:** [`SESSION_HANDOFF.md`](SESSION_HANDOFF.md) (short “where we left off”; this file is the durable product/deploy/agent map).

**Repo:** `/opt/bytelord/projects/imap-spamfilter`  
**Remote:** `github.com:bytecave/imap-spamfilter.git` (branch `main`)  
**Upstream fork of:** marcelverdult/imap-spamfilter  

**Mandatory at session start (with [`SESSION_HANDOFF.md`](SESSION_HANDOFF.md)):**

1. Read **this entire file**.
2. Read **`SESSION_HANDOFF.md`** (current “continue here”).
3. **Supermemory is mandatory:** `supermemory_search` with `container=project` (and `supermemory_list` for recent items) before answering prior-work, scoring, or “what’s next” questions. Search seeds and capture rules are in **Agent onboarding §3**. Do not skip because this file already summarizes the topic.

Then follow **Agent onboarding** below.

---

## Snapshot in one paragraph

Self-hosted IMAP spam filter (Python + Rspamd + Redis + Unbound) on ByteLord. Mailboxes authenticate through sibling **`email-oauth2-proxy`** (XOAUTH2 to M365); this filter speaks plain IMAP `LOGIN` to the proxy. Allow/block lists + one shared Bayes notebook (`defaults.bayes_user: bytelord`) are live. **`rich_bytecave` (rich@bytecave.net) is `mode: move` as of 2026-09-28 01:26 Pacific. The other nine accounts stay `shadow`.** `move_grace_seconds` is 0. Score-based moves do **not** train Bayes. **`flag_untrained_junk` is true in the live `accounts.yml` defaults**, so all ten accounts set the follow-up flag on Junk mail that is new since the Junk bookmark and has not been taught. **Train-Ham** copies the message to the Inbox before learning, then still archives the Train-Ham message in Trained-Ham; that Inbox copy is not score-moved to Junk. Subdomain `@host` matching and `url_suspect` `word_dots = false` are committed and in the running image (rebuilt 2026-09-28 19:22 Pacific). `@kickstarlaunch.com` is on the domain block list for all four roster domains (SQLite only; not in git). Neural stays off. Bayes was not wiped. **The full code and security review is done (2026-09-29, `CLAUDE_FABLE5.1_CODE_REVIEW.md` / `CLAUDE_FABLE5.1_CODE_FIXED.md`), and its fixes are committed but not deployed.** The most important finding: `expire = 0` in `classifier-bayes.conf` had switched rspamd's Bayes expiry *on*, deleting about 374,700 rare tokens since 2026-09-23. **Next: deploy that config fix and rebuild `spamfilter` (SESSION_HANDOFF "continue here"), decide whether to rebuild the Bayes notebook, then the planned Outlook add-in. Do not promote other accounts. Do not wipe Bayes without that decision.** The add-in is specified in `outlook-addin/` and has no code yet.

---

## How this project works

### Runtime topology

Four containers on Docker network **`spamnet`** (shared with email-oauth2-proxy):

| Container | Role |
|---|---|
| `spamfilter-redis` | Bayes / fuzzy / neural persistence |
| `spamfilter-unbound` | Local recursive DNS (DNSBL-friendly) |
| `spamfilter-rspamd` | `/checkv2` scoring + `/learnspam` `/learnham` |
| `spamfilter` | This repo’s Python service (one thread per account) |

Compose **source of truth in git:** `deploy/bytelord-compose.yaml`  
**Live deploy path:** `/opt/bytelord/compose/imap-spamfilter/compose.yaml`  
**Secrets:** `/opt/bytelord/secrets/imap-spamfilter.env` (never commit)  
**SQLite state:** `/opt/bytelord/data/imap-spamfilter/state/` (dir `0700`, DB/WAL/SHM `0600`)  
**Accounts:** `/opt/bytelord/projects/imap-spamfilter/accounts.yml` (**gitignored**)

### Per-account loop (mental model)

1. Drain Train-Spam → learn → Trained-Spam. Drain Train-Ham: **copy to Inbox first** (fingerprint only; bytes unchanged), then learn → Trained-Ham. The Inbox copy stays.
2. Drain Allowlist / Blocklist (person From only) → list upsert/flip → **learn** → MOVE (**Allow→Inbox**, **Block→Junk**).
3. `scan_inbox`: UIDs above Inbox `scan_bookmark`; score with rspamd (including already-`\Seen`); list hits still score, then override routing; over-threshold (`threshold`, default 8) acts by mode (`shadow` log / `flag` / `move`+grace).
4. `execute_due_moves` (Inbox→Junk) and `execute_due_rescues` (Junk→Inbox rescues).
5. `poll_junk` (~`junk_poll_interval`, live **30s**): user Inbox→Junk learns; provider-delivered Junk is **scored** (not learned as spam); score **&lt; `rescue_below`** (default **4**) or allowlisted may be **rescued** in move mode only (mid-band 4–8 stays in Junk). When `flag_untrained_junk` is on, a new Junk UID that stays and has not been taught gets `\Flagged`. The Junk scan itself stays read-only; the flag is a second writable select.
6. Retention / prune when due; IDLE wait (or `poll_interval` if no IDLE).

**Bookmarks:** first sight of a folder/uidvalidity records max UID and **skips historic mail**. That still applies to Inbox and Junk.

**Bayes learns only from explicit teaching:** Train-*, Inbox↔Junk user moves, Allowlist/Blocklist drags. Quiet under-threshold mail and auto-rescue do **not** train.

### Modes (accounts.yml)

| Mode | Behavior |
|---|---|
| `shadow` | Score/log; no auto Inbox/Junk/Trash spam MOVE; Train-* + list drains still MOVE; provider-Junk rescue **logs `would_rescue` only**. `flag_untrained_junk` may still set `\Flagged` on new untrained Junk |
| `flag` | Shadow + `\Flagged` on over-threshold Inbox |
| `move` | Flag + after `move_grace_seconds` MOVE Inbox→Junk when score ≥ `threshold`; provider-Junk rescue MOVEs when score &lt; `rescue_below` or allowlisted |

Live config: **`rich_bytecave` is `move`**; every other account is **shadow**. `learn_grace_seconds: 30`. `move_grace_seconds: 0`. **`accounts.yml` is loaded once at process start — restart `spamfilter` after YAML changes.** The image does not need a rebuild for a mode change (`accounts.yml` is bind-mounted). Move and flag modes run retention; shadow does not.

### Dashboard

- Loopback **`127.0.0.1:8099`** (container EXPOSE 8099).
- NetBird Caddy: **`https://spam.bytelord.net`** (tls internal / private IP bind — same pattern as other ByteLord admin sites).
- Users: hashed file `/opt/bytelord/data/imap-spamfilter/state/dashboard_users` (or env). Restart after creating first user.
- Messages: pagination 200, newest first; score bands `=0`, `<4`, `4-8`, `8-19`, `>=20`; **Trained Spam/Ham/\***; **Untrained**; class both/spam/ham.
- **2026-09-22:** optional **`RSPAMD_WEBUI_URL`** env var (`https://spam.bytelord.net/rspamd/`) shows a "Rspamd" nav link, opens in a new tab, visible to every logged-in user (not admin-gated — it's just a link out). Empty by default → link hidden. Not a proxy, not an auto-login: Rspamd's own password screen (`RSPAMD_PASSWORD`) still gates the WebUI exactly as before.

---

## Git

| Item | Value |
|---|---|
| `origin/main` HEAD | `git log -1` — pushed 2026-09-22 (this file's own commit SHA isn't listed below to avoid a self-reference that goes stale on every amend) |
| Working tree | Check `git status`. As of the 2026-09-23 handoff refresh, `SESSION_HANDOFF.md` and this file may be dirty (findings + next steps) until the operator asks to commit. |
| Branch | `main` tracking `origin/main` (the only development branch; the 2026-09-25 work was fast-forwarded onto it). |

**2026-09-25 commits (oldest first; on `main`):**

| SHA | Summary |
|---|---|
| `842cad0` | Add Claude Opus 5.5 extra code review findings |
| `44819b3` | CR-001: require a byte-identical copy before expunging a list-folder drag |
| `2dd08fc` | CR-002: give up on poison messages instead of halting scans forever |
| `bb83eac` | CR-003: never rescue a Microsoft-flagged spoof out of Junk |
| `35de033` | CR-005: let waitress admit full dashboard list saves |
| `4862aab` | CR-006: select the learn-time Bayes notebook on every scan |
| `64edd2d` | CR-007: learn spam when a user re-junks a rescued message |
| `a3c291d` | CR-008: do not rescue mail the user moved into Junk long after it arrived |
| `eb2c27e` | CR-009: keep unseen, pending, and allowlisted Junk out of retention |
| `931d4fd` | CR-010: score and route mail that has no Message-ID |
| `ba83356` | CR-011: begin SQLite write transactions IMMEDIATE |
| `df419f6` | CR-012: reset reconnect backoff only after a successful pass |
| `953efab` | CR-013: check the hourly learn budget before fetching bodies |
| `4eebc57` | CR-014: do not re-move Train-* copies the server left after MOVE |
| `bf5f051` | CR-016: log pending_move_canceled only for real cancellations |
| `9120008` | CR-017: render bootstrap secret configs under umask 077 |
| `38e757c` | CR-015: cover the core learning, safe-mode, and move-quota paths |
| `292642d` | Document review fixes and correct README drift |
| `0d4573d` | CR-002 refinement: require two healthy probes before give-up |
| later | FIXED/handoff/status docs; allowlisted spoof-suspect flag; compose `TZ: America/Los_Angeles` + `stop_grace_period: 90s` |
| `1d04635` | Use rspamd's stock fuzzy rule (CR-029) |
| `200e06c` | Record deploy + Cursor V1–V7 verification |
| `ddca9c8` | Rescue provider Junk only when first score &lt; 4 (`rescue_below`) |
| `0b982e9` | Expunge Train-* copies Microsoft leaves behind after MOVE |

**2026-09-22 commits (newest last):**

| SHA | Summary |
|---|---|
| `873cd59` | Bump Rspamd to 4.2.0; add optional Rspamd WebUI link to dashboard |
| *(this file)* | Rewrite IMPLEMENTATION_STATUS.md: onboarding rewrite + 2026-09-22 session |

**2026-09-21 commits:**

| SHA | Summary |
|---|---|
| `615457a` | Dashboard port 8080 → **8099** (compose, Dockerfile, Unraid, docs) |
| `076b992` | Messages **Untrained** + score-band / class / pagination UX |
| `67862f6` | Score seen mail + list hits; Allow/Block learn+dest; provider-Junk score/rescue |
| `06c3638` | Project Cursor rule: graphify query **`--dfs --budget 3333`** |
| `75a19ae` | Drop obsolete `.ai-memory.toml` |

**Never commit:** live `accounts.yml`, `/opt/bytelord/secrets/*`, token caches, SQLite under `data/`.

---

## What we accomplished

### Already on `main` before 2026-09-21

- Slices 1–8: hybrid shadow, FETCH size cap, Inbox bookmark, `tls_mode`, IMAP UID identity, rspamd From/Rcpt, secrets/bootstrap, dashboard hardening.
- Slices 9–12: allow/block lists + shared Bayes + list folders + dashboard lists.
- ChatGPT CR **IMAP-CR-001…019** dispositioned in [`CHATGPT_CODE_REVIEW.md`](CHATGPT_CODE_REVIEW.md); valid fixes shipped (CR-002 ignore; CR-016 accepted risk).
- Phase 2 Bayes wipe + `bootstrap_train.py --all-trained` (ops history).
- Contradictory learn skip (`learn_skipped_list`) for allow+spam / block+ham.

### 2026-09-29 session (Claude Fable 5.1 code and security review, on ByteLord)

1. **Review.** I read the docs and plans oldest to newest, all of `filter.py`, and (with two read-only subagents) the dashboard, the CLIs, rspamd `local.d` against the rspamd 4.2.0 sources, compose and CI. Live checks were read-only: SQLite `?mode=ro`, `docker logs`, one Unbound config file, and two non-secret environment variables. 32 findings, traced requirement → architecture → acceptance → code → tests, are in [`CLAUDE_FABLE5.1_CODE_REVIEW.md`](CLAUDE_FABLE5.1_CODE_REVIEW.md).
2. **High findings.**
   - **FABLE-CR-001:** `expire = 0` enabled rspamd Bayes expiry, and about 374,700 tokens were deleted. This is the cause of the 78,042 → 60,919 `RS*` drop.
   - **FABLE-CR-002:** a Message-ID ending in `{n}` made the Train leftover, Train-Ham and list drains hang the IMAP session, which wedged the account's loop on every pass.
   - **FABLE-CR-003:** an 8-bit From or first To/Cc address made every rspamd POST fail, which stalled the scan for 10+ minutes and then left the message unscored.
3. **Fixed (25, one commit each, with tests).** The details are in [`CLAUDE_FABLE5.1_CODE_FIXED.md`](CLAUDE_FABLE5.1_CODE_FIXED.md). Behaviour changes to know about:
   - scans send `Pass: all`;
   - a Train-Ham copy of Junk mail is held as `ham_restored` and still block-list-checked;
   - oversize Train-Ham is copied to the Inbox;
   - a dashboard list Save writes only the loaded list and returns 409 if it changed meanwhile;
   - the ByteLord image is tagged `imap-spamfilter:bytelord`.
4. **Not changed (operator decisions):**
   - rebuilding the Bayes notebook;
   - Unbound recursion (FABLE-CR-005: Spamhaus/URIBL are refused through Cloudflare today);
   - per-account M365 AR trust (FABLE-CR-011);
   - dashboard cookie `Secure`;
   - rspamd hardening.
5. **Tests:** 429 → **487 passed**. 47 of the new tests fail against the pre-review `f53586a`.
6. **Second pass, same day (operator-approved):**
   - Unbound recurses itself (FABLE-CR-005). It was forwarding to Cloudflare, which Spamhaus refuses.
   - Blocklist rules cleaned up (FABLE-CR-032):
     - the local duplicate Spamhaus rule is removed (the stock rule does it properly);
     - Abusix is removed (it needs a key);
     - SpamCop is weighted 1.5.
   - `allow_file_and_shm_inputs = false` on the normal, controller and proxy workers.
   - rspamd's "already learned" 404 counts as `already`.
   - Per-account `m365_auth_trust` switch, default true (FABLE-CR-011).
   - Dashboard cookie `Secure` on ByteLord (FABLE-CR-030).
   - Tests: **501 passed**.
   - DB backed up (`spamfilter.db.bak-20260929-013308-before-bayes-rescue`).
   - Deleted Items rescue confirmed feasible, read-only (SESSION_HANDOFF).
   - **Standing rule from the operator: never change rspamd code, only `local.d` configuration.**

### 2026-09-25 session (Claude Opus 5.5 extra code review — cloud session, no VPS access)

1. **Review:** read every doc in order (upstream README, the 27-document chronological archive, ChatGPT CR, README, this file, and the handoff), then every source/config file. Findings are traced requirement → architecture → acceptance → implementation → tests in [`CLAUDE_OPUS5.5_EXTRA_CODE_REVIEW.md`](CLAUDE_OPUS5.5_EXTRA_CODE_REVIEW.md). Key claims were reproduced with scripts, or checked against rspamd 4.2.0, waitress 3.0.2, and imapclient 3.1.0 source.
2. **High findings.**
   - **CR-001:** list-drain de-dup could **delete** a dragged message on a Message-ID collision.
   - **CR-002:** one poison message **permanently halted** Inbox scanning / Junk learning for its account.
   - **CR-003:** provider-Junk rescue could pull a **Microsoft-flagged spoof** of an allowlisted address back into Inbox.
   - **CR-004:** rspamd **neural autotrains on unadjusted scores** and on every re-scan, which undercuts bucket B (operator approved the fix: `autotrain = false`, neural keys deleted).
3. **Fixed (15, each with tests):** CR-001, 002, 003 (rescue side), 005, 006, 007, 008, 009, 010, 011, 012, 013, 014 (Train-* side), 016, 017. Also new coverage for core learning paths (CR-015) and README corrections. Details and deferrals are in [`CLAUDE_OPUS5.5_EXTRA_CODE_FIXED.md`](CLAUDE_OPUS5.5_EXTRA_CODE_FIXED.md).
4. **Tests:** 349 → **397 passed**; `filter.py` branch coverage 72% → 78%.
5. **Operator follow-ups (same day):** allowlisted Inbox mail that Microsoft marks as spoofed gets `\Flagged` (red flag; flag/move modes) plus an `allowlisted_spoof_suspect` event. `deploy/bytelord-compose.yaml` now has `TZ: America/Los_Angeles` and `stop_grace_period: 90s`. Tests: **401 passed**.
6. **CR-029 (found during deploy):** `rspamd/local.d/fuzzy_check.conf` had a made-up `encryption_key`, so rspamd 4.2.0 dropped the rspamd.com fuzzy rule and fuzzy never scored. The file is now comments only, so the stock rule applies.
7. **Deployed 2026-09-25 by the operator** with the SESSION_HANDOFF runbook: steps 1–7, plus 4b for the fuzzy file. They report that all outputs matched:
   - 4 neural keys deleted (3 `rn_*` + 1 `rn3_*`), 0 left.
   - Bayes `RS*` key count **78042**, unchanged.
   - Redis backup at `/home/bytecave/spamfilter-redis-before-neural-wipe-*.rdb`.
   - Live compose = repo; filter rebuilt.

   **Independent verification by Cursor (SESSION_HANDOFF V1–V7): done 2026-09-24 night.** Pass except Bayes `RS*` key count was 60919 (below the deploy note’s 78042); notebook intact. Later the same night: `rescue_below`, Train-* leftover expunge, and third Bayes retrain (see next section).

### 2026-09-24 night / 2026-09-25 early morning (Cursor — rescue_below, Train leftovers, retrain3)

1. **`rescue_below` (`ddca9c8`):** provider Junk rescued only when first score &lt; 4; Inbox still junked at ≥ 8. User Inbox↔Junk drags never automoved back.
2. **Train-* leftover root cause:** Exchange MOVE-as-COPY. Operator had to delete dozens of Train-* copies by hand. Fix `0b982e9`: `_move_clearing_source` + `_clear_train_leftovers`. Tests **406 passed**. Deployed (rebuild/recreate spamfilter).
3. **Poisoned Trained-Ham:** morning one-shot `/tmp/rescore_trained_spam.py` moved 981 Trained-Spam → Trained-Ham while Bayes was empty (score &lt; 6). Script deleted. Committed training code never score-moves into Trained-*. Operator corrected folders by hand.
4. **Retrain3:** wipe bytelord Bayes only (bak `dump.rdb.bak-20260924-retrain3`); rspamd restarted to clear cached learn counts; `bootstrap_train.py --all-trained` in place. Bayes **spam ≈ 940 / ham ≈ 1595**. Fuzzy in Trained-*: 3× `FUZZY_DENIED` (rich_rjmetalfab). Trained-* dashboard rescored.

### 2026-09-21 session (this status refresh)

1. **Ops / dashboard access:** dashboard on **8099**; Caddy site `spam.bytelord.net` (NetBird-only).
2. **Messages UX:** newest-first pagination; spam/ham/both; Trained Spam/Ham/\*; **Untrained** = not Train-\*, not Inbox↔Junk user learns, not Allow/Block folders, not allowlisted action, not pending_learn, not unlearnable.
3. **Inbox scoring:** remove “only score UNSEEN” gate for new UIDs above bookmark; catch up unscored Inbox rows (cap 50/pass) without moving bookmark.
4. **List hits:** still override routing; now **run `/checkv2`** so scores appear on the dashboard.
5. **Provider Junk:** score new Junk above Junk bookmark; **do not** Bayes-learn from Microsoft’s junking; in **move** mode rescue under-threshold or allowlisted to Inbox after `move_grace_seconds`; blocklisted never rescued; shadow/flag → `would_rescue` only.
6. **Allow/Block drags:** Allowlist → person-allow + **ham learn** + MOVE **Inbox**; Blocklist → person-block + **spam learn** + MOVE **Junk** (learn failure leaves mail in the list folder for retry).
7. **Agent tooling:** ByteLord default `graphify query "…" --dfs --budget 3333` (project `.cursor/rules/graphify.mdc` + `~/.claude/CLAUDE.md` + graphify skill).
8. **Tests:** Docker pytest **334 passed**; live `spamfilter` rebuilt/recreated healthy.

### 2026-09-22 session (Rspamd 4.2.0 + WebUI link)

Full plan at `~/.cursor/plans/rspamd_4.2.0_upgrade_and_webui_link_18e83c16.plan.md`. Researched live via GitHub release notes for 4.1.4/4.1.5/4.2.0 (not from training-data memory).

1. **Why bump:** a controller auth bypass on a malformed password hash (4.1.4, critical), an arbitrary file-read via message-source directives (4.1.5, gated by `allow_file_and_shm_inputs`), controller auth-failure rate limiting (4.1.5), and a batch of UCL/PDF/archive/HTML/CSS DoS-hardening fixes (4.2.0) since we feed rspamd arbitrary attacker-controlled bytes every scan. Detection-quality wins: SVG/OOXML/DOCX attachment-content symbols, `R_DKIM_ALIGNED` split from `R_DKIM_ALLOW` (may interact with the M365 rewrite false-positive investigation in What's next item 3), a real public-suffix-list rework, and hidden-text (`R_WHITE_ON_WHITE`) detection fixes. No `/checkv2`/`/learnspam`/`/learnham` contract change found; single unsharded Redis Bayes is unaffected by the 4.0.0 Ring Hash migration.
2. **Pin bumped** `rspamd/rspamd:4.1.3` → `4.2.0` in `deploy/bytelord-compose.yaml`, `docker-compose.yml`, `unraid/spamfilter-rspamd.xml`, `README.md`, `design-arch/slice7_ops_secrets_supply_chain.md`.
3. **Rspamd WebUI reachability:** read the live `/etc/caddy/Caddyfile` directly (not assumed) — `spam.bytelord.net` was a bare `reverse_proxy 127.0.0.1:8099`. Verified the shipped Rspamd WebUI (`docker exec spamfilter-rspamd`, `/usr/share/rspamd/www/`) uses only relative asset paths (`./css/...`, `./js/...`), so a path-based mount is safe. **Applied live** (operator ran a staged, reviewed script — see below): `spam.bytelord.net` now has `redir /rspamd /rspamd/ permanent` + `handle_path /rspamd/* { reverse_proxy 127.0.0.1:11334 }` + `handle { reverse_proxy 127.0.0.1:8099 }` (dashboard route unchanged, just became the catch-all). Confirmed proxied requests still hit the controller as the Docker bridge gateway, not literal `127.0.0.1` — `secure_ip = "127.0.0.1"` in `worker-controller.inc` is **not** bypassed; Rspamd's own password screen still gates it.
4. **Auto-login investigated and dropped.** Read the shipped auth JS (`js/app/rspamd.js`, `js/app/common.js`) directly: the password only ever reaches `sessionStorage` via the login-form submit handler; nothing reads `location.hash`/`location.search` for auth. No URL-based auto-login hook exists in stock Rspamd WebUI. Building one means patching and maintaining a fork of that file across every future Rspamd bump — decided against it. The link opens the login screen; browser saved-password autofill handles the rest after the first manual login.
5. **Compose:** `spamfilter-rspamd` now publishes `127.0.0.1:11334:11334` (ByteLord compose; generic `docker-compose.yml` got the same as a commented-out block, matching the `8099` pattern). `spamfilter` service gets `RSPAMD_WEBUI_URL: https://spam.bytelord.net/rspamd/` in the ByteLord compose.
6. **Dashboard code:** `filter/dashboard.py` reads `RSPAMD_WEBUI_URL` (empty by default → link hidden), threads it into `render()`'s template context, and the nav template shows `<a href="{{ rspamd_webui_url }}" target="_blank" rel="noopener noreferrer">Rspamd ↗</a>` when set — visible to every logged-in user, not admin-gated. Added 3 tests to `filter/test_dashboard.py` (hidden when unset, present with correct `href`/`target`/`rel` when set, visible to non-admin viewers too). Docker pytest: **337 passed**.
7. **Caddy patch script:** staged at `/tmp/patch_caddy_rspamd.sh` (not committed — lives outside the repo, and outside `/tmp`'s normal lifetime — re-create from this doc if it's gone). Exact-byte-match on the live block before touching anything, idempotent, timestamped backup, `--dry-run` and `--rollback` modes, `sudo caddy validate` before `sudo systemctl reload caddy` (graceful — this host's `ExecReload=caddy reload --force`, doesn't interrupt other Caddy sites). Caught and fixed two real bugs while testing it against the live file before handing it off: a double-stdin-redirect that would have fed the whole Caddyfile to Python as a script, and silent trailing-newline loss from `$(...)` command substitution. **Operator ran it; the Caddy side is live and reloaded.**
8. **Deployed live and verified.** The repo's `deploy/bytelord-compose.yaml` is the source of truth; the deployed copy at `/opt/bytelord/compose/imap-spamfilter/compose.yaml` is a **separate file that does not auto-sync** — it was out of date (still `4.1.3`) until synced by hand this session (`cp` + backup + `docker compose config` validate) before pulling/recreating. Both `spamfilter-rspamd` and `spamfilter` were pulled/recreated (`SPAMFILTER_UID=1001 SPAMFILTER_GID=1001`) and verified: `docker exec spamfilter-rspamd rspamd --version` → `4.2.0`; a real `/checkv2` scan from inside the `spamfilter` container returned the expected `score`/`action`/`symbols` shape; all 10 accounts reconnected (`connected, delimiter='/', mode=shadow, IMAP IDLE=yes`) with no errors; `docker exec spamfilter env` confirms `RSPAMD_WEBUI_URL` landed. End-to-end through Caddy: `curl -sk https://spam.bytelord.net/rspamd` → `301` → `/rspamd/` → `200`; `https://spam.bytelord.net/` (dashboard) still `302` to login, unaffected. Deliberately did **not** run a live `/learnspam`/`/learnham` smoke test — that would have injected test data into the real shared `bytelord` Bayes notebook, which isn't easily reversible; the classifier config and scan-path contract were already sufficient proof.

**Still pending:**
- A day of shadow-mode score-watching after the rspamd bump before touching thresholds/modes (new symbols in 4.2.0 can shift totals either way).

### Current product policy (reopenable when asked)

| Topic | Policy |
|---|---|
| List vs score | List hit **scores**; list **overrides routing**; list hit alone does **not** Bayes-learn |
| Headers matched | From + Sender only (not Reply-To) |
| Precedence | user address → user `@host` → domain address → domain `@host`; `@host` includes subdomains and the longer host wins within that step; allow wins only on true tie. **Live** since the 2026-09-28 image rebuild (`8d310b9` and later). |
| `URL_OBFUSCATED_TEXT` word-dot *(2026-09-28)* | **Live and committed.** `rspamd/local.d/url_suspect.conf` sets `word_dots = false` so “Green Dot Bank” is not scored as a URL (+9). Other obfuscation patterns stay on. |
| `flag_untrained_junk` *(2026-09-28 evening)* | Builtin default **false**. Live `accounts.yml` defaults set it **true for all ten accounts**, including shadow. New Junk UIDs above the bookmark that have not been taught get the single `\Flagged` follow-up flag. Mail already at or below the bookmark is left alone. A user Inbox→Junk drag (pending or completed spam learn) is not flagged. Move-mode rescues that are about to leave Junk are not flagged. |
| Train-Ham restore *(2026-09-28 evening; corrected 2026-09-29)* | Before ham learn, `COPY` the Train-Ham message to the Inbox and store a SHA-256 fingerprint (`our_action=inbox_copied` on the Train-Ham row; `scan_inbox` marks the Inbox copy `ham_restored` when it recognises that fingerprint). There is no UIDPLUS pre-mark: imapclient returns no COPYUID. The restore is recognised *before* the Junk→Inbox revert check, so a copy of mail that sat in Junk is scored, list-checked and held, not learned twice. Only its first Inbox arrival counts. Oversize mail is COPYed without a fingerprint, and ham stays in Train-Ham until its copy exists (FABLE-CR-008/009). Message bytes are not edited. An identical copy already in the Inbox is not copied again. Successful learn still MOVEs the Train-Ham message to Trained-Ham. `scan_inbox` stores the score but does not shadow, flag, or queue Junk for that Inbox copy. A block-list hit still forces Junk. Older ham teaches that lack `inbox_copied` are not held. Train-Spam is unchanged. The copy runs even when the hourly learn budget is spent; a failed copy leaves the message in Train-Ham and skips the learn that pass. |
| `@kickstarlaunch.com` *(2026-09-28)* | Domain block on `bytecave.net`, `bytelord.net`, `eizenhoefer.net`, and `rjmetalfab.com` (live SQLite, not git). Covers that host and subdomains. List hit overrides routing and does not Bayes-learn. Move mode sends new mail to Junk; shadow only logs it. Mail already scored before the insert was not re-routed. |
| Allowlist drag | Upsert/flip + **ham learn** + MOVE → **Inbox** |
| Blocklist drag | Upsert/flip + **spam learn** + MOVE → **Junk** |
| Provider Junk | Score; no spam-learn from “landed in Junk”; rescue when score **&lt; `rescue_below`** (default **4**) or allowlisted (**move** only); mid-band stays in Junk. Never train on rescue. *(2026-09-25)* Spoofed allowlisted mail is not rescued (`m365_spoof_verdict`); INTERNALDATE &gt; 3 days → no rescue. User re-junk of a rescued message **is** learned as spam. |
| Thresholds *(2026-09-24 night)* | `accounts.yml` `threshold` (Inbox→Junk, default 8) and `rescue_below` (provider Junk→Inbox, default 4). Rspamd `local.d/actions.conf` greylist/add_header/reject are **cosmetic** only. |
| Train-* leftovers *(2026-09-24 night)* | Exchange often keeps a source copy after MOVE. Drain expunges it after MOVE; later passes expunge an old leftover only when a byte-identical copy is verified in Trained-* (`train_leftover_expunged`). |
| Junk retention *(2026-09-25)* | Only Junk UIDs `poll_junk` has processed; skips pending learns and allowlisted / in-flight-rescue rows (protects allowlisted provider-Junk in `flag` mode) |
| Poison message *(2026-09-25)* | After 5 failed passes over ≥ 10 min, and only when a synthetic probe proves rspamd healthy on two consecutive passes, the UID is `scan_giveup` (left in place) and scanning moves on. An rspamd outage still halts. |
| No Message-ID *(2026-09-25)* | Filtered normally by IMAP identity; `no_message_id` audit event only |
| Allowlisted spoof suspect *(2026-09-25)* | Allow still wins (stays in Inbox). If the trusted Microsoft AR says spoofed, `\Flagged` is set (flag/move) and `allowlisted_spoof_suspect` is logged; shadow only logs. |
| Contradictory Train/move | allow+spam / block+ham → `learn_skipped_list` |
| Rspamd fuzzy *(2026-09-25)* | Stock `rspamd.com` rule (local file is comments only). Before CR-029 it never loaded. |
| Rspamd neural *(2026-09-25)* | `autotrain = false`; neural Redis keys deleted during deploy → no `NEURAL_*` score. **Stays off by decision** (next section). |
| Caps | `max_list_per_run=100`, `max_list_entries=1000` |
| Bayes token lifetime *(2026-09-29)* | Tokens never expire. `classifier-bayes.conf` must not set `expire`: any number turns on rspamd's `bayes_expiry`, and `0` deletes rare tokens (FABLE-CR-001; `test_config_files.py` guards it). **Not deployed yet.** |
| DNS for blocklists *(2026-09-29)* | Unbound resolves from the root servers (`unbound/forward-records.conf` mounted over the image's Cloudflare forwarder). Stock Spamhaus ZEN/DBL, SURBL and URIBL rules score. Local `rbl.conf` adds only SpamCop (`Received:` hops, weight 1.5 in `rbl_group.conf`) (FABLE-CR-005/032). **Not deployed yet.** |
| Microsoft auth trust *(2026-09-29)* | Per-account `m365_auth_trust`, default `true`. Set it `false` for any mailbox Microsoft 365 does not deliver to; that account then ignores `Authentication-Results` (no bucket B, no spoof verdict) (FABLE-CR-011). |
| rspamd changes *(2026-09-29)* | **Configuration only (`rspamd/local.d/`); never patch rspamd code.** File/shm inputs are off on every worker. |
| rspamd evaluation *(2026-09-29)* | `/checkv2` sends `Pass: all`, so every rule runs even past `reject = 15`. `actions.conf` is then truly cosmetic (FABLE-CR-004). **Not deployed yet.** |
| Dashboard list Save *(2026-09-29)* | Writes only the (scope, kind) the page loaded (hidden fields). If the list or its sibling changed since the page was opened, it returns 409 and writes nothing (FABLE-CR-006/007). **Not deployed yet.** |

Older docs that say “list hits skip `/checkv2`” or “both list drains MOVE to Inbox” are **stale** — trust this file + `README.md` + `filter/filter.py`.

### Neural: why it stays off (decided 2026-09-25)

Rspamd's neural module is a second-opinion model. It does **not** read message text. Its inputs are *which other rules fired* (Bayes, fuzzy, RBL, auth, MIME symbols), and it learns which combinations mean spam.

The operator asked for neural only if it demonstrably improves accuracy **without** adding false-positive/negative risk. Standard neural cannot promise that here, so it stays off. **No dashboard nudge, threshold detector, or auto-enable was built, deliberately.** The reasons:

1. **It adds no new information.** Bayes already learns from exactly the same human actions (Train-\* folders, Inbox↔Junk drags, list drags). Neural would re-learn those same labels one level up, from the other rules' outputs. Changes that genuinely add information look like CR-029 fuzzy (rspamd.com's view of mail seen elsewhere).
2. **Its inputs are noisy on this IMAP path.** There is no SMTP client IP or HELO, and bucket B zeroes auth symbols only *after* rspamd returns. Neural would train on the raw auth-recheck noise.
3. **It adds FP/FN risk.** Any extra scorer can push borderline mail across the threshold.
   - A model trained on about 1,000 human-labelled spam messages from a handful of campaigns can learn "looks like bulk mail = spam" and add points to legitimate newsletters and receipts.
   - It retrains itself, so its behavior drifts, especially after config or symbol changes.
   - `explain_score.py` would show only `NEURAL_SPAM`, never *why*.
4. **There isn't enough data.** `max_trains = 1000` means no model is built until there are 1,000 examples of each class. Trained-Spam had about 205 learns on 2026-09-24.

**If the operator ever asks to revisit this,** the only zero-risk path is a *watch-only trial*:
- Keep `autotrain = false`.
- Train only from human actions. rspamd 4.2.0 accepts an `ANN-Train: spam|ham` request header on `/checkv2` (`src/plugins/lua/neural.lua`), so the filter would send one when it teaches Bayes. That is new code in `try_learn` and a bootstrap from Trained-\*.
- Give `NEURAL_SPAM`/`NEURAL_HAM` weight 0 (for example in `local.d/neural_group.conf`), so the symbols appear but never change a score.
- Compare neural's verdicts with the operator's later Train-\* decisions, and give it weight only if it catches spam Bayes misses without touching ham.
- Prerequisite: at least about 1,000 human-labelled spam **and** ham.

rspamd 4.2.0 neural can also take LLM/embedding "providers" that read message *content*. That variant is the one most likely to add real accuracy, but it needs an embedding model: mail content sent to a cloud API, or a local model on the VPS. It is a separate project with privacy and resource trade-offs, not a config switch.

---

## Live VPS state

**Filter container:** local image tag `ghcr.io/marcelverdult/imap-spamfilter:latest` today (baked from `/opt/bytelord/projects/imap-spamfilter/filter`, never pushed). From the next deploy of `deploy/bytelord-compose.yaml` it is **`imap-spamfilter:bytelord`** (FABLE-CR-010), so `docker compose pull` cannot replace it with upstream's public image. Recreate with uid/gid **1001**.

```bash
export SPAMFILTER_UID=1001 SPAMFILTER_GID=1001
docker compose -f /opt/bytelord/compose/imap-spamfilter/compose.yaml build spamfilter
docker compose -f /opt/bytelord/compose/imap-spamfilter/compose.yaml up -d --force-recreate --no-deps spamfilter
# verify:
docker compose -f /opt/bytelord/compose/imap-spamfilter/compose.yaml ps
ls -la /opt/bytelord/data/imap-spamfilter/state
```

**Do not** `compose down` redis/rspamd/unbound casually — Bayes lives in Redis.

**Deployed 2026-09-28 01:26 Pacific:** `accounts.yml` only. `rich_bytecave` `mode: move`. Filter restarted, not rebuilt. Connected log shows `mode=move` for that account and `mode=shadow` for the other nine. Retention then moved old Trained-* mail to Deleted Items (see SESSION_HANDOFF). Bayes backup is in the Redis data directory, named `dump.rdb.bak-20260928-before-rich-move` and `appendonlydir.bak-20260928-before-rich-move`.

**Deployed 2026-09-25 (operator, SESSION_HANDOFF runbook):**
- Filter rebuilt from `main` `1d04635` (Opus review fixes).
- Live compose = `deploy/bytelord-compose.yaml`, with `TZ=America/Los_Angeles` and `stop_grace_period: 90s`; the previous file is kept as `compose.yaml.bak.*`.
- Live `rspamd/local.d/neural.conf` has `autotrain = false`, and `fuzzy_check.conf` is the comments-only version.
- Neural Redis keys deleted (Bayes untouched: `RS*` = 78042). Redis backup at `/home/bytecave/spamfilter-redis-before-neural-wipe-*.rdb`.
- Verification commands: SESSION_HANDOFF "Cursor: first job" V1–V7.

**Deployed 2026-09-22:** `rspamd/rspamd:4.2.0` is live (`spamfilter-rspamd` recreated and healthy), `spamfilter-rspamd` publishes `127.0.0.1:11334`, and `spamfilter` was recreated with `RSPAMD_WEBUI_URL` set — all verified (see "2026-09-22 session" above for the exact checks). **Important gotcha hit this session:** the deployed compose file at `/opt/bytelord/compose/imap-spamfilter/compose.yaml` is a **separate copy that does not auto-sync** from the repo's `deploy/bytelord-compose.yaml` source of truth — a bare `docker compose pull` against the deployed path silently pulled the *old* `4.1.3` because the deployed file still said so. Always diff and `cp` the source of truth over before pulling/recreating:

```bash
diff -u /opt/bytelord/compose/imap-spamfilter/compose.yaml \
        /opt/bytelord/projects/imap-spamfilter/deploy/bytelord-compose.yaml
# review the diff, then if it's only your intended change:
cp /opt/bytelord/compose/imap-spamfilter/compose.yaml \
   /opt/bytelord/compose/imap-spamfilter/compose.yaml.bak.$(date +%Y%m%d-%H%M%S)
cp /opt/bytelord/projects/imap-spamfilter/deploy/bytelord-compose.yaml \
   /opt/bytelord/compose/imap-spamfilter/compose.yaml
docker compose -f /opt/bytelord/compose/imap-spamfilter/compose.yaml config >/dev/null && echo OK
```

Then the normal recreate:

```bash
docker compose -f /opt/bytelord/compose/imap-spamfilter/compose.yaml pull spamfilter-rspamd
docker compose -f /opt/bytelord/compose/imap-spamfilter/compose.yaml up -d --no-deps spamfilter-rspamd
docker exec spamfilter-rspamd rspamd --version   # confirm the version you expect

export SPAMFILTER_UID=1001 SPAMFILTER_GID=1001
docker compose -f /opt/bytelord/compose/imap-spamfilter/compose.yaml up -d --force-recreate --no-deps spamfilter
docker compose -f /opt/bytelord/compose/imap-spamfilter/compose.yaml ps
```

Leave `spamfilter-redis` alone throughout — Bayes state is untouched by either recreate. Watch scores in shadow for a day before touching thresholds/modes; 4.2.0 adds new symbols (OOXML/SVG attachment content, `R_DKIM_ALIGNED`) that can shift totals.

**Dashboard:** `127.0.0.1:8099` or `https://spam.bytelord.net` (NetBird). SSH tunnel if needed:

```powershell
ssh -L 8099:127.0.0.1:8099 bytecave@bytelord
```

**Python:** filter image is **3.12** (imapclient 3.1.0 breaks on 3.14 with `tls_mode: none`).

---

## Live accounts (`accounts.yml`, gitignored)

All accounts use proxy LOGIN with `password: "Dummy"`, `imap_host: email-oauth2-proxy`, port **1993**, `tls_mode: none`, `allow_insecure_tls: true`. **`rich_bytecave` is `mode: move` (2026-09-28). The other nine are `shadow`.**

Notable defaults (verify in file — operators edit live YAML):

- `bayes_user: bytelord`
- `learn_grace_seconds: 30`
- `junk_poll_interval: 30`
- `move_grace_seconds: 0`
- Roster domains: `bytecave.net`, `eizenhoefer.net`, `rjmetalfab.com`, `bytelord.net`
- `# threshold: 14.0` commented — live junk line is the builtin **8**, rescue line is builtin **4**. `trained_retention_days` is unset, so the builtin **7** applies and is now active for `rich_bytecave`.

| `name` | mailbox | `actual_name` |
|---|---|---|
| `rich_bytecave` | rich@bytecave.net | Rich Eizenhoefer |
| `rich_rjmetalfab` | rich@rjmetalfab.com | Rich Eizenhoefer |
| `steve_rjmetalfab` | steve@rjmetalfab.com | Steve Jones |
| `bobbi_rjmetalfab` | bobbi@rjmetalfab.com | Bobbi Naugle |
| `marilyn_rjmetalfab` | marilyn@rjmetalfab.com | Marilyn Arnold |
| `rich_eizenhoefer` | rich@eizenhoefer.net | Rich Eizenhoefer |
| `shon_bytecave` | shon@bytecave.net | Shon Eizenhoefer |
| `nac_bytecave` | nac@bytecave.net | Shon Eizenhoefer |
| `shon_eizenhoefer` | shon@eizenhoefer.net | Shon Eizenhoefer |
| `sam_bytecave` | sam@bytecave.net | Sam Anderson |

**10 accounts.** Do **not** wire `bytelord.net` mailboxes through the OAuth proxy unless asked (ordinary IMAP; not M365).

---

## Agent onboarding (new session checklist)

### 0. Global ByteLord rules + mandatory reads

**Before any other work in this repo:**

1. Read **`IMPLEMENTATION_STATUS.md`** (this file) in full.
2. Read **`SESSION_HANDOFF.md`** (where the last session stopped).
3. Call **`supermemory_search`** (`container=project`) and skim **`supermemory_list`**. Skipping Supermemory because the docs “already say it” is a rule violation.

Also read **`/home/bytecave/.claude/CLAUDE.md`** (Cursor user rule). It covers Agent Mail, subagent limits (≤2; main session commits), graphify-first research (`graphify query "…" --dfs --budget 3333`), and mandatory Supermemory recall/capture. Machine-local copies: `~/.cursor/rules/graphify.mdc` and `~/.cursor/rules/supermemory.mdc`. There is **no** project `.cursor/rules/graphify.mdc` (removed 2026-09-23).

### 1. Agent Mail (MCP `user-mcp-agent-mail`)

Project key = absolute repo root:

```text
/opt/bytelord/projects/imap-spamfilter
```

1. `macro_start_session` with that `human_key` (program/model as appropriate).
2. Keep the returned agent name for the session.
3. `fetch_inbox` (and before each edit batch).
4. Before editing: `file_reservation_paths(..., exclusive=true)` on exact paths.
5. If `conflicts` non-empty / `granted` empty → **stop**; message the holder or work elsewhere.
6. Subagents do **not** register; reserve their files under **your** name.
7. After commit: `release_file_reservations`.

Full semantics live in CLAUDE.md — do not invent a parallel protocol.

### 2. Graphify (mandatory before broad explore)

```bash
cd /opt/bytelord/projects/imap-spamfilter
graphify query "<architecture question>" --dfs --budget 3333
graphify explain "<symbol or concept>"
graphify path "<A>" "<B>"
# after code edits:
graphify update .
```

Prefer `explain` / `path` for specific symbols; use `query --dfs --budget 3333` for broad architecture. Graph artifacts under `graphify-out/` are **gitignored**. If `graphify-out/needs_update` exists, refresh before trusting doc-derived graph context (see CLAUDE.md).

### 3. Supermemory (mandatory — MCP `plugin-cursor-supermemory-supermemory`)

**Must** search before prior-work / scoring / policy answers. Use **`container=project`** for this codebase; **`user`** only for cross-project preferences.

Search seeds (run at least the first):

- `IMAP-path remediation buckets A B C mx.microsoft.com BROKEN_HEADERS`
- `Amazon UID 235843 DKIM body hash Authentication-Results`
- `imap-spamfilter shadow dashboard 8099 provider junk`
- `compose dual-file SPAMFILTER_UID 1001 RSPAMD 4.2.0`

After a live deploy, a scoring-policy decision, or a root-cause finding, **`supermemory_add`** with `container=project` before ending the turn. Never store secrets or `accounts.yml` contents.

### 4. Tests (no host pytest — use Docker)

```bash
docker run --rm -v /opt/bytelord/projects/imap-spamfilter:/src -w /src/filter \
  python:3.12-slim bash -c \
  "pip install -q -r requirements.txt pytest==8.4.2 && python -m pytest -q --tb=short"
```

**Last known:** **501 passed** (2026-09-29, after the Fable 5.1 fixes and second pass; 429 before). Mount the whole repository as above: tests read `README.md`, `unraid/` and `rspamd/local.d/`.

### 5. Rafter / secrets

Do not dump credentials or write into `/etc/caddy` without approval. Prefer staging scripts under `/tmp` when host config is locked. Evaluate risky shell via Rafter when required by environment policy.

### 6. Commit / push discipline

Only when the user asks. No force-push, no `--no-verify`, no secrets. Main session only commits (not subagents).

---

## 2026-09-23 findings — IMAP rescan vs edge auth (blocker for leaving shadow)

**Problem:** Legit Google Security alerts and Amazon transactional mail score ~18–22 (`reject`) in shadow. That is **not** “M365 headers are ugly” and **not** proof Google/Amazon are spam. Proofpoint/M365 mark the same mail good because they score at **SMTP ingress**. imap-spamfilter **IMAP-fetches the stored copy** and re-runs rspamd `/checkv2` with **no SMTP `Ip`/`Helo`**.

**Evidence (live):**
- Amazon UID `235843` (`auto-confirm@amazon.com`): M365 `Authentication-Results: mx.microsoft.com` → `dkim=pass`, `dmarc=pass`, `spf=pass`. Rspamd recheck → `R_DKIM_REJECT`, `BLACKLIST_DMARC=+6`, etc. **DKIM body hash of IMAP `BODY.PEEK[]` does not match** the `bh=` in the amazon.com signature — stored body ≠ wire body Microsoft verified. Stripping `X-MS-*`/ARC/AR headers does **not** clear the fails.
- Google alerts: same class of auth-recheck + IMAP-path noise; often Gmail CAF forward into M365 as well.
- Among 40 recent scores ≥15: all had `BROKEN_HEADERS` in the top 5; most were ≥60% auth/header symbols.

**Do not trust Microsoft’s spam/junk decision** (SCL / Junk folder) — that is why this project exists. **Do trust Microsoft’s edge SPF/DKIM/DMARC verdict** when stamped as `Authentication-Results` with authserv-id **`mx.microsoft.com`**. Spammers cannot make *that* check return pass without actually passing SPF/DKIM/DMARC (or sending via an authorized path). Residual risks are **not** “spoof MS crypto,” they are: (1) **implementation** — only honor AR from `mx.microsoft.com`, prefer the M365-stamped/outermost header (ignore forged AR from other authserv-ids; be careful if duplicate `mx.microsoft.com` headers exist); (2) **spam that correctly passes auth** (BEC / compromised mailbox) — AR pass is correct; Bayes/URLs/fuzzy/neural/lists must still catch it.

**Remediation buckets (locked product direction 2026-09-23):**

| Bucket | Symbols | Action |
|---|---|---|
| **A — IMAP noise** | `HFILTER_HOSTNAME_UNKNOWN`, `RDNS_NONE` | Safe to zero/downweight. No client IP on this architecture → nearly all mail pays the same penalty; removing it does not favor spam over ham. |
| **B — Auth recheck** | `R_DKIM_REJECT`, `R_SPF_FAIL`, `DMARC_POLICY_*`, `BLACKLIST_DMARC`, related | **Suppress failure weight only when** trusted `mx.microsoft.com` AR says the corresponding check **pass**. If AR says fail / missing / untrusted authserv → **keep** failure symbols. Do **not** blanket-disable auth scoring. Prefer rspamd mechanisms (`trusted_authserv_id`, ARC `whitelisted_signers_map` + `adjust_dmarc` for `microsoft.com`) over crude global score cuts. |
| **C — Content / MIME** | Bayes, URLs, fuzzy, neural, lists, **`BROKEN_HEADERS`** | **Keep the symbol.** The Amazon/Google +8 was `Rcpt: bytelord` (not an address). Scan now uses the mailbox as `Rcpt` and `Delivered-To: bytelord` for the notebook. Smashed headers still score +8. |

Ham training **cannot** cancel A/B auth-header symbols when they still fire (untrusted AR / real fail). Leaving shadow before scores look sane would still auto-Junk some good Inbox mail that was never dashboard-rescored. Content spam that passes Microsoft auth must be taught via Train-Spam / block lists.

---

## What’s next (suggested order)

1. **Deploy the Fable 5.1 review fixes (operator).** First copy `rspamd/local.d/classifier-bayes.conf` into the live `local.d` and restart rspamd (FABLE-CR-001, Bayes expiry). Then sync the compose file and rebuild `spamfilter`. Commands are in `CLAUDE_FABLE5.1_CODE_FIXED.md`, and the test list is in `SESSION_HANDOFF.md`. Then decide whether to rebuild the Bayes notebook, and whether to turn on Unbound recursion (FABLE-CR-005). *(The full code and security review itself is done: `CLAUDE_FABLE5.1_CODE_REVIEW.md`.)*
2. **Outlook add-in remains planned.** Requirements and setup notes are in `outlook-addin/` (`d988d40`). There is no add-in code yet. Do not treat that as a filter defect, and do not start the add-in until the review is done.
3. **`rich_bytecave` is in `move`.** The first retention sweep (01:26 Pacific) moved 101 Trained-Spam and 500 Trained-Ham older than ~8 days to Deleted Items. Default `trained_retention_days` is 7, so later hourly sweeps of up to 500 may have continued. Do not promote any other account. CR-014 Inbox→Junk leftover check is still open.
4. **CR-004 / neural: done and closed.** It stays off. See "Neural: why it stays off" before proposing any change.
5. **Do not wipe Bayes again** unless asked. Restore point: `dump.rdb.bak-20260928-before-rich-move` and `appendonlydir.bak-20260928-before-rich-move` inside the Redis data volume. Do not run any score-based Trained-* mover.
6. **`@host` subdomain matching and `word_dots = false` are committed and in the running image.** Do not rebuild `spamfilter` unless asked.
7. **Operator decisions still open:** CR-019 `Rcpt` = mailbox vs first To/Cc; optional `DASHBOARD_TRUSTED_PROXIES`.
8. **Rspamd 4.2.0 / WebUI-link deploy is done** (2026-09-22). V1–V7 deploy verification is done.
9. **ChatGPT CR-016 / supply chain** (accepted risk): lock and hash deps, image digests, GHA SHA pins — when prioritized.
10. More M365 mailboxes only with an Exchange grant + proxy section + YAML. No generic IMAP for `bytelord.net` unless asked.
11. Optional polish: ChatGPT CR disposition items, and the Low items under "Not fixed" in `CLAUDE_OPUS5.5_EXTRA_CODE_FIXED.md`. Not release blockers. Human testing table in SESSION_HANDOFF is still open, now against one live move-mode mailbox.

---

## Key files for a new agent

| File | Why |
|---|---|
| `IMPLEMENTATION_STATUS.md` | **Mandatory** — this file |
| `SESSION_HANDOFF.md` | **Mandatory** — current continue-here note (2026-09-28 evening: Train-Ham restore, untrained-Junk flag, review next) |
| `code_review_orientation.md` | How to run a code review here (the 2026-09-29 one is done) |
| `CLAUDE_FABLE5.1_CODE_REVIEW.md` | 2026-09-29 review: 32 traced findings (FABLE-CR-001…032) |
| `CLAUDE_FABLE5.1_CODE_FIXED.md` | 2026-09-29 fixes (25), deferrals, deploy steps, validation |
| `filter/test_fable_review_fixes.py`, `filter/test_config_files.py` | Regression tests for the 2026-09-29 fixes |
| `README.md` | Operator docs (modes, folders, dashboard, safe-mode) |
| `CHATGPT_CODE_REVIEW.md` | Prior CR findings + disposition |
| `CLAUDE_OPUS5.5_EXTRA_CODE_REVIEW.md` | 2026-09-25 review: 29 traced findings (OPUS-CR-001…029) |
| `CLAUDE_OPUS5.5_EXTRA_CODE_FIXED.md` | 2026-09-25 fixes (15 code + 2 rspamd config), deferrals, validation |
| `filter/test_opus_review_fixes.py` | Regression tests for the 2026-09-25 fixes |
| `design-arch/allow_block_sliced_plan.md` | List/Bayes product decisions (reopenable) |
| `design-arch/slice9_shared_bayes.md` … `slice12_dashboard_lists.md` | List specs (note policy drift vs 2026-09-21 — verify against code) |
| `design-arch/slice3_inbox_bookmark.md` | Bookmark / terminal-prefix rules |
| `filter/filter.py` | Core filter |
| `filter/dashboard.py` | Dashboard |
| `filter/bootstrap_train.py` / `explain_score.py` | Ops tools |
| `filter/test_*.py` | Regression suite |
| `deploy/bytelord-compose.yaml` | Compose source of truth |
| `/home/bytecave/.claude/CLAUDE.md` | ByteLord-wide agent protocol (graphify + supermemory mandatory) |
| `/home/bytecave/.cursor/rules/*.mdc` | Machine-local Cursor globals (graphify, supermemory) |

Sibling project: `/opt/bytelord/projects/email-oauth2-proxy` (OAuth / M365 IMAP bridge).

---

## Graphify cheat sheet

```bash
cd /opt/bytelord/projects/imap-spamfilter
graphify query "How does poll_junk rescue provider junk?" --dfs --budget 3333
graphify explain "scan_inbox"
graphify path "poll_junk" "try_learn"
graphify update .
```
