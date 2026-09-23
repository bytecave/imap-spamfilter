# Implementation status — imap-spamfilter (ByteLord)

**Last updated:** 2026-09-22  
**Audience:** brand-new agent sessions (Cursor / Claude Code / Codex) with no prior chat memory.  
**Companion:** [`SESSION_HANDOFF.md`](SESSION_HANDOFF.md) (short “where we left off”; this file is the durable product/deploy/agent map).

**Repo:** `/opt/bytelord/projects/imap-spamfilter`  
**Remote:** `github.com:bytecave/imap-spamfilter.git` (branch `main`)  
**Upstream fork of:** marcelverdult/imap-spamfilter  

**Read this first** before exploring the tree. Then follow **Agent onboarding** below.

---

## Snapshot in one paragraph

Self-hosted IMAP spam filter (Python + Rspamd + Redis + Unbound) on ByteLord. Mailboxes authenticate through sibling **`email-oauth2-proxy`** (XOAUTH2 to M365); this filter speaks plain IMAP `LOGIN` to the proxy. Allow/block lists + one shared Bayes notebook (`defaults.bayes_user: bytelord`) are live. **All accounts remain `mode: shadow`** (scan/log; no auto Inbox→Junk), but Train-* and Allowlist/Blocklist drains still MOVE. **2026-09-21:** dashboard moved to **8099** / `https://spam.bytelord.net` (NetBird); Messages got Untrained + score-band filters; Inbox scores already-`\Seen` mail; list hits are scored; provider-Junk can be scored and (in **move** mode) rescued; Allow/Block drags learn + route. **2026-09-22:** Rspamd bumped `4.1.3` → `4.2.0` and **live** (both `spamfilter-rspamd` and `spamfilter` recreated, verified: `rspamd --version` 4.2.0, a real `/checkv2` scan, all 10 accounts reconnected cleanly); dashboard gained an optional **`RSPAMD_WEBUI_URL`** nav link that opens Rspamd's own built-in WebUI in a new tab; Caddy routes `spam.bytelord.net/rspamd/` → the Rspamd controller, verified end-to-end (`/rspamd` → 301 → `/rspamd/` → 200; dashboard `/` unaffected). Working tree is **clean on `origin/main`** as of this update (see Git section for the exact commits). Full filter pytest: **337 passed** (2026-09-22, includes 3 new WebUI-link tests). **Next preferred step: full code + security review by Claude Code or Codex** (see What’s next).

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
| Working tree | **Clean**, pushed 2026-09-22 |
| Branch | `main` tracking `origin/main` |

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
| Provider Junk | Score; no spam-learn from “landed in Junk”; rescue under threshold or allowlisted (**move** only); never train on rescue |
| Contradictory Train/move | allow+spam / block+ham → `learn_skipped_list` |
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

### 0. Global ByteLord rules

Read **`/home/bytecave/.claude/CLAUDE.md`** at session start (Cursor user rule requires it). It covers:

- Agent Mail (required on ByteLord git repos)
- Subagent limits (≤2; main session commits)
- Graphify-first research + **`graphify query "…" --dfs --budget 3333`**

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

Prefer `explain` / `path` for specific symbols; use `query --dfs --budget 3333` for broad architecture. Project rule: `.cursor/rules/graphify.mdc`. Graph artifacts under `graphify-out/` are **gitignored**. If `graphify-out/needs_update` exists, refresh before trusting doc-derived graph context (see CLAUDE.md).

### 3. Supermemory (MCP `plugin-cursor-supermemory-supermemory`)

Use **project** container for this codebase; **user** for cross-project preferences.

Useful search seeds:

- `imap-spamfilter dashboard 8099 Untrained`
- `provider junk rescue allow block learn`
- `compose SPAMFILTER_UID 1001`
- `LEARN unlearnable`

After hard bugs or policy decisions, `supermemory_add` with `container: project`.

### 4. Tests (no host pytest — use Docker)

```bash
docker run --rm -v /opt/bytelord/projects/imap-spamfilter:/src -w /src/filter \
  python:3.12-slim bash -c \
  "pip install -q -r requirements.txt pytest==8.4.2 && python -m pytest -q --tb=short"
```

**Last known:** **334 passed** (2026-09-21).

### 5. Rafter / secrets

Do not dump credentials or write into `/etc/caddy` without approval. Prefer staging scripts under `/tmp` when host config is locked. Evaluate risky shell via Rafter when required by environment policy.

### 6. Commit / push discipline

Only when the user asks. No force-push, no `--no-verify`, no secrets. Main session only commits (not subagents).

---

## What’s next (suggested order)

1. **Full code + security review of this project by Claude Code or Codex** (primary next step). Scope should include `filter/filter.py`, `filter/dashboard.py`, list/Bayes paths, provider-Junk rescue, Allow/Block learn+MOVE, state permissions, compose/secrets handling, and dashboard auth. Produce findings with severity + recommended fixes; do **not** auto-promote accounts out of shadow based on the review alone.
2. **Rspamd 4.2.0 / WebUI-link deploy is done** (2026-09-22) — both containers recreated and verified, `spam.bytelord.net/rspamd/` loads, committed and pushed. Watch scores in shadow for a day before touching thresholds/modes given the new 4.2.0 symbols.
3. **Stay in shadow** until scores and rescues look sane in the dashboard; then promote carefully `flag` → `move` when the operator asks.
4. **IMAP-path symbol remediation** for M365 rewrite false positives (`BROKEN_HEADERS`, `BLACKLIST_DMARC`, SPF/DKIM after rewrite) via rspamd `local.d` weights; use `explain_score.py` before/after — worth re-checking once 4.2.0's `R_DKIM_ALIGNED` split is live, it may shift or explain some of these.
5. **CR-016 / supply-chain** (accepted risk): lock+hash deps, image digests, GHA SHA pins — when prioritized.
6. More M365 mailboxes only with Exchange grant + proxy section + YAML. No generic IMAP for `bytelord.net` unless asked.
7. Optional polish called out in CR disposition (fingerprint caps, SQLite CHECKs, etc.) — not release blockers.

---

## Key files for a new agent

| File | Why |
|---|---|
| `IMPLEMENTATION_STATUS.md` | This file — start here |
| `SESSION_HANDOFF.md` | Short continuity note (may lag; prefer this file for 2026-09-21+) |
| `README.md` | Operator docs (modes, folders, dashboard, safe-mode) |
| `CHATGPT_CODE_REVIEW.md` | Prior CR findings + disposition |
| `design-arch/allow_block_sliced_plan.md` | List/Bayes product decisions (reopenable) |
| `design-arch/slice9_shared_bayes.md` … `slice12_dashboard_lists.md` | List specs (note policy drift vs 2026-09-21 — verify against code) |
| `design-arch/slice3_inbox_bookmark.md` | Bookmark / terminal-prefix rules |
| `filter/filter.py` | Core filter |
| `filter/dashboard.py` | Dashboard |
| `filter/bootstrap_train.py` / `explain_score.py` | Ops tools |
| `filter/test_*.py` | Regression suite |
| `deploy/bytelord-compose.yaml` | Compose source of truth |
| `.cursor/rules/graphify.mdc` | Graphify explore rule |
| `/home/bytecave/.claude/CLAUDE.md` | ByteLord-wide agent protocol |

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
