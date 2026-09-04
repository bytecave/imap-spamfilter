# Implementation status — imap-spamfilter (ByteLord)

**Last updated:** 2026-09-04  
**Supersedes for current work:** [`SESSION_HANDOFF.md`](SESSION_HANDOFF.md) (that file is still useful for VPS layout and OAuth, but its “what’s next” and mailbox list are stale).

**Repo:** `/opt/bytelord/projects/imap-spamfilter`  
**Remote:** `github.com:bytecave/imap-spamfilter.git` (branch `main`)  
**Upstream fork of:** marcelverdult/imap-spamfilter  

Read this first in a new agent/chat session before exploring the tree.

---

## Snapshot in one paragraph

Allow/block lists + one VPS Bayes notebook (design-arch slices 9–12) are **implemented and running** on the live `spamfilter` container. Architecture docs were committed and tagged; **the implementation is still uncommitted local work**. All live mailboxes are M365 via `email-oauth2-proxy`, still in **shadow**. Dashboard list editors work (admin only). Follow-up UX: Save no longer warns, search highlights without stealing focus, Messages/Learned column sort. User is unhappy with Bayes score quality (upstream rspamd/project behavior, not a slice-9–12 bug). **Do not** wire `bytelord.net` mailboxes through the OAuth proxy.

---

## Git

| Item | Value |
|---|---|
| `origin/main` HEAD | `a0b8897` — “Add allow/block list architecture slices 9–12.” |
| Tag | `before-allow-block-list` (annotated; “Before Allow/Block List”) at that commit |
| Implementation | **Not committed, not pushed.** Working tree has the slice 9–12 code plus later dashboard UX. |

**Uncommitted (commit only if the user asks):**

- Modified: `README.md`, `accounts.yml.example`, `filter/Dockerfile`, `filter/dashboard.py`, `filter/filter.py`, `filter/test_connection.py`, `filter/test_dashboard.py`, `filter/test_learn.py`, `filter/test_shadow_mode.py`
- Untracked: `filter/lists.js`, `filter/test_address_lists.py`, `filter/test_list_folders.py`

**Never commit:** live `accounts.yml` (gitignored), `/opt/bytelord/secrets/*`, token caches.

---

## What we accomplished

### Earlier (already on `main`)

Slices 1–8 (hybrid shadow, FETCH cap, inbox bookmark, `tls_mode`, IMAP UID identity, rspamd From/Rcpt, secrets/bootstrap, dashboard hardening). OAuth proxy PoC, then more M365 mailboxes. Parent plan: `design-arch/sliced_plan_code_review_fixes.md`.

### This push of work (local, uncommitted)

Architecture: `design-arch/allow_block_sliced_plan.md` + `slice9_shared_bayes.md` … `slice12_dashboard_lists.md`. **`new_requirements.md` is historical; slices win on disagreement.**

| Slice | Spec | Status |
|---|---|---|
| 9 | One VPS Bayes (`defaults.bayes_user: bytelord`) | Done (live YAML + docs; did **not** change `BUILTIN_DEFAULTS["bayes_user"]` or `classifier-bayes.conf`) |
| 10 | Roster, parser, `address_lists`, Inbox match | Done |
| 11 | `INBOX/Allowlist` / `INBOX/Blocklist` drain | Done |
| 12 | Admin Domain/User list editors | Done |

**Locked product policy (do not reopen):**

- Lists override **routing only**. Still scan; **never Bayes-learn** from list hits.
- Scan From + Sender + Reply-To. IMAP drag writes **From only**.
- Person lists by exact `actual_name`; domain lists from YAML roster.
- Allow wins same-specificity ties; log `list_conflict`.
- After IMAP drag: upsert/flip then **MOVE mail back to Inbox**.
- Inbox scan only (not Junk poll). Caps: `max_list_per_run=100`, `max_list_entries=1000`.
- Roster type (`company`/`personal`) is v1 metadata only.
- IMAP drags persist immediately. Dashboard Save is the only batched editor.

**Dashboard UX after slice 12 (also uncommitted):**

- Save: clear dirty on submit so the browser does not show “Leave site?”
- Find-in-list: keep caret in the search box; highlight **all** matching textarea lines via overlay; clear highlights when query is empty or has no matches
- Messages: click **Score** to sort (SQL); first click high→low, click again toggles
- Learned: click **Event** to sort (SQL); first click A→Z, click again toggles
- `script-src 'self'`; `filter/lists.js` served as `/lists.js`

**Tests:** last full dashboard file run **47 passed**; earlier full `filter/` suite was **176 passed** (then more tests were added). Host has no pytest/`ensurepip`. Use:

```bash
docker run --rm -v /opt/bytelord/projects/imap-spamfilter/filter:/app -w /app \
  python:3.12-slim bash -c \
  "pip install -q -r requirements.txt pytest==8.4.2 && python -m pytest -q --tb=short"
```

After code edits: `graphify update .` (graph in `graphify-out/`, gitignored).

---

## Live VPS state

**Filter image** is built from the local `filter/` tree (`compose` build context). Last rebuild included list editors, search overlay, and column sort.

**Compose / data (unchanged layout):**

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

Then http://127.0.0.1:8080/ — hard-refresh after JS/CSS deploys (`Ctrl+F5`).

Rebuild/restart filter (as user `bytecave`, uid/gid 1001):

```bash
export SPAMFILTER_UID=1001 SPAMFILTER_GID=1001
docker compose -f /opt/bytelord/compose/imap-spamfilter/compose.yaml up -d --build spamfilter
```

Proxy:

```bash
docker compose -f /opt/bytelord/compose/email-oauth2-proxy/compose.yaml up -d --force-recreate
```

Python **3.12** in the filter image (imapclient 3.1.0 + 3.14 breaks `tls_mode: none`).

Do **not** use YAML aliases like `*secrets-file:/path:ro` in Compose (go-yaml rejects alias+suffix).

---

## Live accounts (`accounts.yml`, gitignored)

All `mode: shadow`. Proxy LOGIN with `password: "Dummy"`, `imap_host: email-oauth2-proxy`, port `1993`, `tls_mode: none`, `allow_insecure_tls: true`.

`defaults.bayes_user: bytelord` (one notebook). Roster domains: `bytecave.net`, `eizenhoefer.net`, `rjmetalfab.com`, `bytelord.net`.

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

**10 accounts.** Allowlist/Blocklist folders were auto-created on connect.

### bytelord.net is not M365

`bytelord.net` is **ordinary IMAP** (user/password to a normal server), not Exchange / OAuth2. It stays on the **domain roster** for allow/block lists only.

We briefly added `rich@bytelord.net` and `kyle@bytelord.net` via the OAuth proxy; Exchange returned `User is authenticated but not connected.` User confirmed they are not M365. **Backed out** of `accounts.yml`, proxy config, and `tokenstore.config`. Do not add them again until a real IMAP/password path exists.

Proxy currently has **no** `[rich@bytelord.net]` / `[kyle@bytelord.net]` sections. If the proxy crash-loops with `No section: 'rich@bytelord.net'`, leftover rows in `/opt/bytelord/data/email-oauth2-proxy/cache/tokenstore.config` are the usual cause — remove those sections and recreate the proxy container.

### Adding another M365 mailbox

1. Exchange: `Add-MailboxPermission` for the spamfilter app (same as existing mailboxes).
2. Copy a CCG `[user@domain]` block in `/opt/bytelord/secrets/email-oauth2-proxy.config` (same tenant/client/secret as `rich@bytecave.net`).
3. Matching `accounts.yml` entry (`actual_name` required, `mode: shadow` first).
4. Restart **proxy then** `spamfilter`.

Gmail / live.com: still deferred (not client-credentials).

---

## Standing rules (agent + product)

- **graphify first:** `.cursor/rules/graphify.mdc` — `graphify query "…" --budget 10000` before exploring; `graphify update .` after code edits.
- **Commit/push** only when the user asks. No force-push, no hook skip, do not commit secrets.
- **Do not edit the plan file** unless asked (`allow_block_sliced_plan.md` status table may still say “ready to implement”; code is ahead of that table).
- Slices 9–12 **win** over `new_requirements.md`.
- Caddy / Netbird / `spam.bytelord.net` is **out of scope** for these slices.
- Filter talks IMAP `LOGIN` only. OAuth lives in **email-oauth2-proxy**, not this repo.
- Live `accounts.yml` is gitignored; `accounts.yml.example` is the tracked template.
- Dashboard list POST is admin-only + CSRF; login body limit stays 16 KiB; list POST 256 KiB.
- Shadow: no Inbox/Junk/Trash auto-junk; Train-* and Allowlist/Blocklist drain + MOVE-back-to-Inbox are allowed.

---

## What’s next (suggested)

1. **User may want a commit/push** of the uncommitted implementation (slices 9–12 + dashboard UX). Ask first; use the repo’s commit-message style; do not include `accounts.yml`.
2. **Bayes quality** — user reports an ~1125-item notebook still scores most spam low and flags some ham. They framed this as the upstream project, not our list work. Optional later: confirm Trained-* re-feed via `bootstrap_train.py` **without** `--move-to` (slice 9 follow-up; not done this session), threshold when leaving shadow, more ham/spam training. Stay in **shadow** until scores look sane; then `flag` then `move`.
3. **Do not** implement generic IMAP user/password for `bytelord.net` unless asked (new auth path).
4. More M365 mailboxes only with Exchange grant + proxy section + YAML.
5. Dashboard: Domain/User lists and Score/Event sort are live after rebuild; no further list-slice work unless the user files bugs.
6. Deferred from older handoff: container lockdown, Redis LRU, GHA SHA pins, oversize MIME, Entra cert instead of client secret.

---

## Key files for a new agent

| File | Why |
|---|---|
| `IMPLEMENTATION_STATUS.md` | This file |
| `design-arch/allow_block_sliced_plan.md` | Locked list/Bayes product decisions |
| `design-arch/slice9_shared_bayes.md` … `slice12_dashboard_lists.md` | Implementation specs |
| `design-arch/sliced_plan_code_review_fixes.md` | Slices 1–8 + deferred ops |
| `filter/filter.py` | Lists, scan, IMAP drain, schema |
| `filter/dashboard.py` + `filter/lists.js` | Dashboard + list editor + sort |
| `filter/test_address_lists.py`, `test_list_folders.py`, `test_dashboard.py` | Slice 10–12 + UX tests |
| `deploy/bytelord-compose.yaml` | Filter compose source of truth |
| `accounts.yml` / `accounts.yml.example` | Runtime vs template |
| `.cursor/rules/graphify.mdc` | Explore-via-graphify |

---

## Graphify

```bash
cd /opt/bytelord/projects/imap-spamfilter
graphify query "<architecture question>" --budget 10000
graphify update .
```
