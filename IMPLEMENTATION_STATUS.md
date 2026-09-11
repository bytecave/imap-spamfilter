# Implementation status — imap-spamfilter (ByteLord)

**Last updated:** 2026-09-10  
**Session continuity:** [`SESSION_HANDOFF.md`](SESSION_HANDOFF.md) (next-agent “where we left off”; keep this file for durable product/deploy state).

**Repo:** `/opt/bytelord/projects/imap-spamfilter`  
**Remote:** `github.com:bytecave/imap-spamfilter.git` (branch `main`)  
**Upstream fork of:** marcelverdult/imap-spamfilter  

Read this first in a new agent/chat session before exploring the tree.

---

## Snapshot in one paragraph

Allow/block lists + one VPS Bayes notebook (design-arch slices 9–12) are **on `origin/main`**, with live mailboxes still in **shadow** via `email-oauth2-proxy`. **2026-09-10:** ChatGPT code review findings **IMAP-CR-001…019** were triaged and dispositioned in [`CHATGPT_CODE_REVIEW.md`](CHATGPT_CODE_REVIEW.md). Valid findings are **implemented in the working tree** (not yet committed/pushed). Live `spamfilter` image was **rebuilt and recreated** from local `filter/`; SQLite state dir is now **0700** and DB/WAL/SHM **0600** (IMAP-CR-011). Full filter pytest: **286 passed**. CR-002 ignored (out of scope). CR-016 remains **Accepted Risk** (supply-chain pins deferred). Product policy for allow/block lists remains **reopenable whenever asked**.

---

## Git

| Item | Value |
|---|---|
| `origin/main` HEAD (last known) | `4e2d994` — “Code review results, Cursor plans, misc” |
| Prior markers | `953b756` ai-memory marker; `b991f71` contradict-learn skip; tag `before-allow-block-list` at `a0b8897` |
| **Working tree (2026-09-10)** | **Uncommitted** CR fixes + tests + docs (see below). Do **not** commit/push unless the user asks. |

**Modified (tracked):** `.github/workflows/build.yml`, `README.md`, `filter/{filter,dashboard,bootstrap_train,explain_score}.py`, `filter/requirements.txt` (Flask **3.1.3**), several `filter/test_*.py`, `unraid/bootstrap.sh`.

**Untracked (keep; do not discard):** `CHATGPT_CODE_REVIEW.md` (includes **Code Review Disposition**), `filter/test_code_review_findings.py`, `imap-spamfilter-plans-2026-09-08/`, `.gitattributes`, `deploy/fix-cursor-apparmor.txt`.

**Never commit:** live `accounts.yml` (gitignored), `/opt/bytelord/secrets/*`, token caches.

---

## What we accomplished

### Earlier (already on `main`)

Slices 1–8 (hybrid shadow, FETCH cap, inbox bookmark, `tls_mode`, IMAP UID identity, rspamd From/Rcpt, secrets/bootstrap, dashboard hardening). Allow/block + shared Bayes (slices 9–12). List-skip scan (`cc1eb29`). Contradictory learn skip (`b991f71`). Phase 2 corpus wipe + `--all-trained` re-feed (ops, 2026-09-06).

### 2026-09-10 — ChatGPT code review disposition + deploy

Source review: [`CHATGPT_CODE_REVIEW.md`](CHATGPT_CODE_REVIEW.md). Disposition section documents every ID.

| ID | Disposition | Notes |
|---|---|---|
| IMAP-CR-001 | Fixed | Allowlist cancels `pending_move`; due-move re-checks list policy before MOVE |
| IMAP-CR-002 | No Change Necessary | Operator: ignore; unrelated AppArmor helper |
| IMAP-CR-003 | Fixed | Inbox body-SHA `message_fingerprints` across prune horizon |
| IMAP-CR-004 | Fixed | Typed YAML strings; `ConfigError` instead of bare `SystemExit` |
| IMAP-CR-005 | Fixed | Dashboard list cap from YAML defaults; `url_for` redirects |
| IMAP-CR-006 | Fixed | Dashboard catches `ConfigError` (503 card) |
| IMAP-CR-007 | Fixed | Bootstrap `--move-to` updates `current_folder` |
| IMAP-CR-008 | Fixed | Ambiguous Message-ID refused; events snapshot subject |
| IMAP-CR-009 | Fixed | Catch rate = moves / routing decisions (bounded) |
| IMAP-CR-010 | Fixed | Rspamd `/stat` schema validation |
| IMAP-CR-011 | Fixed + **live** | State `0700`, DB/WAL/SHM `0600`; container rebuilt 2026-09-10 |
| IMAP-CR-012 | Fixed | `explain_score` uses `fetch_under_cap` |
| IMAP-CR-013 | Fixed | Learn KPIs “30d”; account heartbeat + config roster health |
| IMAP-CR-014 | Fixed | Bootstrap fails closed without positive UIDVALIDITY |
| IMAP-CR-015 | Fixed | Flask 3.1.3; CI `pip-audit`; `Vary: Cookie` |
| IMAP-CR-016 | Accepted Risk | Transitive lock / image digests / action SHAs still deferred |
| IMAP-CR-017 | Fixed | List enum validation at Db write boundary |
| IMAP-CR-018 | Fixed | Hard cap on `score_detail_json` incl. huge `action` |
| IMAP-CR-019 | Fixed | README rate-limit vs safe-mode aligned + contract test |

**Tests (Docker `python:3.12-slim`, mount repo at `/src`):**

```bash
docker run --rm -v /opt/bytelord/projects/imap-spamfilter:/src -w /src/filter \
  python:3.12-slim bash -c \
  "pip install -q -r requirements.txt pytest==8.4.2 && python -m pytest -q --tb=short"
```

**Result (2026-09-10):** `286 passed`. Also: `pip-audit -r filter/requirements.txt` clean; `bash -n deploy/*.sh unraid/*.sh` OK.

After code edits: `graphify update .` (graph in `graphify-out/`, gitignored). Graphify may be stale relative to CR edits.

### Product policy (reopenable; unchanged)

- Lists override **routing** and **skip rspamd scan** on hit. Never Bayes-learn from list hits.
- Scan **From + Sender** only. Reply-To ignored.
- User lists (`actual_name`): addresses **and** `@host`. Domain lists: addresses and `@host`.
- Precedence: user address → user `@host` → domain address → domain `@host`. Allow wins only on a true tie.
- After IMAP drag: upsert/flip then **MOVE mail back to Inbox**.
- Caps: `max_list_per_run=100`, `max_list_entries=1000` (dashboard editor uses YAML **defaults** cap).
- Contradictory Train-* / Inbox↔Junk / bootstrap: allow+spam and block+ham skip (`learn_skipped_list`).

---

## Live VPS state

**Filter container (2026-09-10 evening PT):** rebuilt from local `filter/`, recreated, **healthy**. Image tag still named `ghcr.io/marcelverdult/imap-spamfilter:latest` (local bake; not necessarily pushed to GHCR).

**State permissions (verified):**

```text
/opt/bytelord/data/imap-spamfilter/state/   drwx------ (0700)
spamfilter.db / -wal / -shm / heartbeat    -rw------- (0600)
```

**Compose / data layout:**

| Path | Role |
|---|---|
| `/opt/bytelord/projects/imap-spamfilter/` | Git checkout + gitignored `accounts.yml` |
| `/opt/bytelord/projects/email-oauth2-proxy/` | Proxy clone + local Docker overlay |
| `/opt/bytelord/secrets/imap-spamfilter.env` | `RSPAMD_PASSWORD`, `REDIS_PASSWORD` |
| `/opt/bytelord/secrets/email-oauth2-proxy.config` | Proxy INI (mode 600) |
| `/opt/bytelord/data/imap-spamfilter/` | redis, rspamd, SQLite **state** |
| `/opt/bytelord/data/email-oauth2-proxy/cache/` | token store + proxy logs |
| `/opt/bytelord/compose/imap-spamfilter/compose.yaml` | Live filter stack |
| `/opt/bytelord/compose/email-oauth2-proxy/compose.yaml` | Live proxy |

Dashboard: `127.0.0.1:8080` only. From Windows PowerShell:

```powershell
ssh -L 8080:127.0.0.1:8080 bytecave@bytelord
```

Rebuild/restart filter only (as `bytecave`, uid/gid 1001):

```bash
export SPAMFILTER_UID=1001 SPAMFILTER_GID=1001
docker compose -f /opt/bytelord/compose/imap-spamfilter/compose.yaml build spamfilter
docker compose -f /opt/bytelord/compose/imap-spamfilter/compose.yaml up -d --force-recreate --no-deps spamfilter
```

Python **3.12** in the filter image (imapclient 3.1.0 + 3.14 breaks `tls_mode: none`).

---

## Live accounts (`accounts.yml`, gitignored)

All `mode: shadow`. Proxy LOGIN with `password: "Dummy"`, `imap_host: email-oauth2-proxy`, port `1993`, `tls_mode: none`, `allow_insecure_tls: true`.

`defaults.bayes_user: bytelord`. Roster: `bytecave.net`, `eizenhoefer.net`, `rjmetalfab.com`, `bytelord.net`.

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

**10 accounts.** Do **not** wire `bytelord.net` mailboxes through the OAuth proxy (ordinary IMAP; not M365).

---

## Standing rules (agent + product)

- **graphify first:** `.cursor/rules/graphify.mdc` — query before exploring; `graphify update .` after code edits.
- **Commit/push** only when the user asks. No force-push, no hook skip, do not commit secrets.
- Allow/block **product policy is reopenable** whenever asked. Slices 9–12 win over `new_requirements.md`.
- Caddy / Netbird / `spam.bytelord.net` out of scope unless asked.
- Filter talks IMAP `LOGIN` only. OAuth lives in **email-oauth2-proxy**.
- Shadow: no Inbox/Junk/Trash auto-junk; Train-* and Allowlist/Blocklist drain + MOVE-back-to-Inbox are allowed.

---

## What’s next (suggested)

1. **Commit/push** the CR working-tree changes when the user asks (single logical commit or review-sized commits; include `CHATGPT_CODE_REVIEW.md` + `test_code_review_findings.py` if desired).
2. **Stay in shadow** until scores look sane; then `flag` → `move`. Do not promote to `move` until CR-001 build has been live long enough to trust (it is live as of 2026-09-10 rebuild).
3. **IMAP-path symbol remediation** — Amazon-class false positives from `BROKEN_HEADERS` / `BLACKLIST_DMARC` / SPF-DKIM after M365 rewrite; rspamd `local.d` weight overrides; `explain_score.py` before/after; Spamhaus open-resolver when convenient.
4. **Nice-to-have / accepted:** CR-016 supply-chain pins (lock+hash deps, image digests, GHA SHA pins); other disposition “limitations” (fingerprint caps, historical event subjects, SQLite CHECK enums, etc.).
5. More M365 mailboxes only with Exchange grant + proxy section + YAML. No generic IMAP for `bytelord.net` unless asked.

---

## Key files for a new agent

| File | Why |
|---|---|
| `IMPLEMENTATION_STATUS.md` | This file |
| `SESSION_HANDOFF.md` | Next-session pickup |
| `CHATGPT_CODE_REVIEW.md` | Findings + **Code Review Disposition** |
| `filter/test_code_review_findings.py` | CR regression tests |
| `design-arch/allow_block_sliced_plan.md` | List/Bayes product decisions |
| `design-arch/slice9_shared_bayes.md` … `slice12_dashboard_lists.md` | List specs |
| `design-arch/sliced_plan_code_review_fixes.md` | Slices 1–8 + deferred ops |
| `filter/filter.py`, `dashboard.py`, `bootstrap_train.py`, `explain_score.py` | Production code |
| `deploy/bytelord-compose.yaml` | Filter compose source of truth |
| `.cursor/rules/graphify.mdc` | Explore-via-graphify |

---

## Graphify

```bash
cd /opt/bytelord/projects/imap-spamfilter
graphify query "<architecture question>" --budget 10000
graphify update .
```
