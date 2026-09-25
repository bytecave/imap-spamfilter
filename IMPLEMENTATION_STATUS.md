# Implementation status — imap-spamfilter (ByteLord)

**Last updated:** 2026-09-25 (Claude Opus 5.5 extra code review + fixes, on `main`)  
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

Self-hosted IMAP spam filter (Python + Rspamd + Redis + Unbound) on ByteLord. Mailboxes authenticate through sibling **`email-oauth2-proxy`** (XOAUTH2 to M365); this filter speaks plain IMAP `LOGIN` to the proxy. Allow/block lists + one shared Bayes notebook (`defaults.bayes_user: bytelord`) are live. **All accounts remain `mode: shadow`**. **2026-09-22:** Rspamd **4.2.0** live; dashboard WebUI link. **2026-09-24:** IMAP-path buckets **A+B+C live**. A zeros `HFILTER_HOSTNAME_UNKNOWN`/`RDNS_NONE`. B (`apply_m365_auth_trust`) zeros DKIM/SPF/DMARC/`BLACKLIST_DMARC` only from the outermost Microsoft AR (`mx.microsoft.com` or `compauth=` + `protection.outlook.com` Received-SPF). C: bare `bytelord` is `Delivered-To` on scan; mailbox is HTTP `Rcpt` (was the false `BROKEN_HEADERS` +8). Shared Bayes wiped and rebuilt from Trained-* (spam≈205, ham≈2410 after re-feed; Redis bak `dump.rdb.bak-20260924`). Dashboard Trained-* rows bulk-rescored into SQLite (~2897); Inbox/top Junk **not** bulk-rescored. Auth-passed content spam can still score low (operator will Train-Spam). Chelsea mistaken ham learn moved back to Trained-Spam. **2026-09-25:** full code/security review done ([`CLAUDE_OPUS5.5_EXTRA_CODE_REVIEW.md`](CLAUDE_OPUS5.5_EXTRA_CODE_REVIEW.md), 28 findings). 15 fixes plus an allowlisted-spoof warning flag and compose `TZ`/`stop_grace_period` are on **`main`** ([`CLAUDE_OPUS5.5_EXTRA_CODE_FIXED.md`](CLAUDE_OPUS5.5_EXTRA_CODE_FIXED.md)); **not deployed yet**. Neural self-training is turned off (`autotrain = false`), with an operator-approved delete of the neural `rn_*`/`rn3_*` Redis keys in the SESSION_HANDOFF runbook. Human testing is pending. **Next: run the SESSION_HANDOFF deploy runbook, test, stay in shadow; promote only when asked.**

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

1. Drain Train-Spam / Train-Ham → learn → Trained-*.
2. Drain Allowlist / Blocklist (person From only) → list upsert/flip → **learn** → MOVE (**Allow→Inbox**, **Block→Junk**).
3. `scan_inbox`: UIDs above Inbox `scan_bookmark`; score with rspamd (including already-`\Seen`); list hits still score, then override routing; over-threshold acts by mode (`shadow` log / `flag` / `move`+grace).
4. `execute_due_moves` (Inbox→Junk) and `execute_due_rescues` (Junk→Inbox rescues).
5. `poll_junk` (~`junk_poll_interval`, live **30s**): user Inbox→Junk learns; provider-delivered Junk is **scored** (not learned as spam); under-threshold or allowlisted may be **rescued** in move mode only.
6. Retention / prune when due; IDLE wait (or `poll_interval` if no IDLE).

**Bookmarks:** first sight of a folder/uidvalidity records max UID and **skips historic mail**. That still applies to Inbox and Junk.

**Bayes learns only from explicit teaching:** Train-*, Inbox↔Junk user moves, Allowlist/Blocklist drags. Quiet under-threshold mail and auto-rescue do **not** train.

### Modes (accounts.yml)

| Mode | Behavior |
|---|---|
| `shadow` | Score/log; no auto Inbox/Junk/Trash spam MOVE; Train-* + list drains still MOVE; provider-Junk rescue **logs `would_rescue` only** |
| `flag` | Shadow + `\Flagged` on over-threshold Inbox |
| `move` | Flag + after `move_grace_seconds` MOVE Inbox→Junk; provider-Junk rescue MOVEs when allowed |

Live config is still **shadow**. `learn_grace_seconds: 30` (was 15). **`accounts.yml` is loaded once at process start — restart `spamfilter` after YAML changes.**

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

### 2026-09-25 session (Claude Opus 5.5 extra code review — cloud session, no VPS access)

1. **Review:** read every doc in order (upstream README, the 27-document chronological archive, ChatGPT CR, README, this file, and the handoff), then every source/config file. Findings are traced requirement → architecture → acceptance → implementation → tests in [`CLAUDE_OPUS5.5_EXTRA_CODE_REVIEW.md`](CLAUDE_OPUS5.5_EXTRA_CODE_REVIEW.md). Key claims were reproduced with scripts, or checked against rspamd 4.2.0, waitress 3.0.2, and imapclient 3.1.0 source.
2. **High findings.**
   - **CR-001:** list-drain de-dup could **delete** a dragged message on a Message-ID collision.
   - **CR-002:** one poison message **permanently halted** Inbox scanning / Junk learning for its account.
   - **CR-003:** provider-Junk rescue could pull a **Microsoft-flagged spoof** of an allowlisted address back into Inbox.
   - **CR-004:** rspamd **neural autotrains on unadjusted scores** and on every re-scan, which undercuts bucket B (operator decision; not changed).
3. **Fixed (15, each with tests):** CR-001, 002, 003 (rescue side), 005, 006, 007, 008, 009, 010, 011, 012, 013, 014 (Train-* side), 016, 017. Also new coverage for core learning paths (CR-015) and README corrections. Details and deferrals are in [`CLAUDE_OPUS5.5_EXTRA_CODE_FIXED.md`](CLAUDE_OPUS5.5_EXTRA_CODE_FIXED.md).
4. **Tests:** 349 → **397 passed**; `filter.py` branch coverage 72% → 78%.
5. **Operator follow-ups (same day):** allowlisted Inbox mail that Microsoft marks as spoofed gets `\Flagged` (red flag; flag/move modes) plus an `allowlisted_spoof_suspect` event. `deploy/bytelord-compose.yaml` now has `TZ: America/Los_Angeles` and `stop_grace_period: 90s`. Tests: **401 passed**.
6. **Not deployed.** Needs `git pull`, a compose copy to the live path, and a filter image rebuild. No Rspamd/Redis config or data changed.

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
| Precedence | user address → user `@host` → domain address → domain `@host`; allow wins only on true tie |
| Allowlist drag | Upsert/flip + **ham learn** + MOVE → **Inbox** |
| Blocklist drag | Upsert/flip + **spam learn** + MOVE → **Junk** |
| Provider Junk | Score; no spam-learn from “landed in Junk”; rescue under threshold or allowlisted (**move** only); never train on rescue. *(2026-09-25)* Rescue is refused for Microsoft-flagged spoofs (trusted outermost AR `compauth=fail`, or `dmarc=fail` without compauth) and for mail whose INTERNALDATE is > 3 days old (user-moved, not delivered). A user re-junking a rescued message **is** learned as spam. |
| Junk retention *(2026-09-25)* | Only Junk UIDs `poll_junk` has processed; skips pending learns and allowlisted / in-flight-rescue rows (protects allowlisted provider-Junk in `flag` mode) |
| Poison message *(2026-09-25)* | After 5 failed passes over ≥ 10 min, and only when a synthetic probe proves rspamd healthy on two consecutive passes, the UID is `scan_giveup` (left in place) and scanning moves on. An rspamd outage still halts. |
| No Message-ID *(2026-09-25)* | Filtered normally by IMAP identity; `no_message_id` audit event only |
| Allowlisted spoof suspect *(2026-09-25)* | Allow still wins (stays in Inbox). If the trusted Microsoft AR says spoofed, `\Flagged` is set (flag/move) and `allowlisted_spoof_suspect` is logged; shadow only logs. |
| Contradictory Train/move | allow+spam / block+ham → `learn_skipped_list` |
| Rspamd neural *(2026-09-25)* | `autotrain = false`; neural Redis keys deleted during deploy → no `NEURAL_*` score unless deliberately retrained |
| Caps | `max_list_per_run=100`, `max_list_entries=1000` |

Older docs that say “list hits skip `/checkv2`” or “both list drains MOVE to Inbox” are **stale** — trust this file + `README.md` + `filter/filter.py`.

---

## Live VPS state

**Filter container:** local image tag `ghcr.io/marcelverdult/imap-spamfilter:latest` (baked from `/opt/bytelord/projects/imap-spamfilter/filter`, not necessarily pushed to GHCR). Recreate with uid/gid **1001**.

```bash
export SPAMFILTER_UID=1001 SPAMFILTER_GID=1001
docker compose -f /opt/bytelord/compose/imap-spamfilter/compose.yaml build spamfilter
docker compose -f /opt/bytelord/compose/imap-spamfilter/compose.yaml up -d --force-recreate --no-deps spamfilter
# verify:
docker compose -f /opt/bytelord/compose/imap-spamfilter/compose.yaml ps
ls -la /opt/bytelord/data/imap-spamfilter/state
```

**Do not** `compose down` redis/rspamd/unbound casually — Bayes lives in Redis.

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

All **`mode: shadow`**. Proxy LOGIN with `password: "Dummy"`, `imap_host: email-oauth2-proxy`, port **1993**, `tls_mode: none`, `allow_insecure_tls: true`.

Notable defaults (verify in file — operators edit live YAML):

- `bayes_user: bytelord`
- `learn_grace_seconds: 30`
- `junk_poll_interval: 30`
- `move_grace_seconds: 0`
- Roster domains: `bytecave.net`, `eizenhoefer.net`, `rjmetalfab.com`, `bytelord.net`
- `# threshold: 14.0` commented — leave alone until leaving shadow

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

**Last known:** **401 passed** (2026-09-25; was 349 before the review).

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

1. **Deploy and test the 2026-09-25 fixes:** `git pull`, copy `deploy/bytelord-compose.yaml` to the live compose path, then rebuild `spamfilter`. The prioritized live test table is in `SESSION_HANDOFF.md` ("Test these first"). The riskiest behavior changes are CR-001 (list-drain de-dup), CR-002 (poison give-up), CR-011 (`BEGIN IMMEDIATE`), CR-013 (learn budget), and the move-mode rescue/retention guards (CR-003/007/008/009).
2. **CR-004 decided:** neural `autotrain = false` is in the repo; the neural key delete is step 4 of the SESSION_HANDOFF runbook. Watch scores for a few days afterwards (no `NEURAL_*` contribution).
3. **Stay in `shadow`.** Watch scores after the A/B/C + Bayes rebuild. The operator will Train-Spam auth-passed content spam (5Tool-style cold pitch, Chelsea, iPic). Before `flag`/`move`, run **one test mailbox in `move` mode** to confirm the rescue/retention guards and whether Exchange leaves Inbox copies after `UID MOVE` (CR-014). Promote only when asked; do **not** auto-promote from a code review alone. Do **not** wipe Bayes again unless asked.
4. **A/B/C + Bayes rebuild + Trained-* dashboard rescore are done** (2026-09-24). Inbox and top-level Junk dashboard rows were intentionally not bulk-rescored; optional later if the operator asks.
5. **Operator decisions still open from the review:** CR-019 `Rcpt` = mailbox vs first To/Cc; optional `DASHBOARD_TRUSTED_PROXIES`. (CR-003 Inbox side → flag instead of move; `TZ`/`stop_grace_period` done; `env_file` intentionally kept.)
6. **Rspamd 4.2.0 / WebUI-link deploy is done** (2026-09-22).
7. **ChatGPT CR-016 / supply chain** (accepted risk): lock and hash deps, image digests, GHA SHA pins — when prioritized.
8. More M365 mailboxes only with an Exchange grant + proxy section + YAML. No generic IMAP for `bytelord.net` unless asked.
9. Optional polish: ChatGPT CR disposition items, and the Low items under "Not fixed" in `CLAUDE_OPUS5.5_EXTRA_CODE_FIXED.md`. Not release blockers.

---

## Key files for a new agent

| File | Why |
|---|---|
| `IMPLEMENTATION_STATUS.md` | **Mandatory** — this file |
| `SESSION_HANDOFF.md` | **Mandatory** — current continue-here note (2026-09-24 evening) |
| `README.md` | Operator docs (modes, folders, dashboard, safe-mode) |
| `CHATGPT_CODE_REVIEW.md` | Prior CR findings + disposition |
| `CLAUDE_OPUS5.5_EXTRA_CODE_REVIEW.md` | 2026-09-25 review: 28 traced findings (OPUS-CR-001…028) |
| `CLAUDE_OPUS5.5_EXTRA_CODE_FIXED.md` | 2026-09-25 fixes (15), deferrals, validation |
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
