# Implementation status — imap-spamfilter (ByteLord)

**Last updated:** 2026-09-06  
**Supersedes for current work:** [`SESSION_HANDOFF.md`](SESSION_HANDOFF.md) (that file is still useful for VPS layout and OAuth, but its “what’s next” and mailbox list are stale).

**Repo:** `/opt/bytelord/projects/imap-spamfilter`  
**Remote:** `github.com:bytecave/imap-spamfilter.git` (branch `main`)  
**Upstream fork of:** marcelverdult/imap-spamfilter  

Read this first in a new agent/chat session before exploring the tree.

---

## Snapshot in one paragraph

Allow/block lists + one VPS Bayes notebook (design-arch slices 9–12) are **implemented and on `origin/main`**. User lists may include `@host` as well as addresses; matching is From + Sender only (Reply-To ignored). User-list hits override roster-scoped domain lists. All live mailboxes are M365 via `email-oauth2-proxy`, still in **shadow**. Shared Bayes re-feed: `bootstrap_train.py --all-trained`. High scores are explainable via `explain_score.py` and `messages.score_detail` (Amazon ~25 was `BROKEN_HEADERS` + `BLACKLIST_DMARC` + auth fails — **not Bayes**). **Do not** wire `bytelord.net` mailboxes through the OAuth proxy. Product policy for allow/block lists is **reopenable whenever asked**.

---

## Git

| Item | Value |
|---|---|
| Previous `origin/main` | `441b951` — “Implement allow/block lists, shared Bayes, and dashboard list UX.” |
| Tag | `before-allow-block-list` (annotated) at `a0b8897` |
| This change | User-list `@host`, From+Sender-only match, user list overrides domain list (commit on `main`) |

**Never commit:** live `accounts.yml` (gitignored), `/opt/bytelord/secrets/*`, token caches.

---

## What we accomplished

### Earlier (already on `main`)

Slices 1–8 (hybrid shadow, FETCH cap, inbox bookmark, `tls_mode`, IMAP UID identity, rspamd From/Rcpt, secrets/bootstrap, dashboard hardening). OAuth proxy PoC, then more M365 mailboxes. Parent plan: `design-arch/sliced_plan_code_review_fixes.md`.

### This push of work (on `main`, plus follow-up matcher policy)

Architecture: `design-arch/allow_block_sliced_plan.md` + `slice9_shared_bayes.md` … `slice12_dashboard_lists.md`. **`new_requirements.md` is historical; slices win on disagreement.** Product policy is **reopenable whenever asked** — do not treat the list below as frozen.

| Slice | Spec | Status |
|---|---|---|
| 9 | One VPS Bayes (`defaults.bayes_user: bytelord`) | Done (live YAML + docs; did **not** change `BUILTIN_DEFAULTS["bayes_user"]` or `classifier-bayes.conf`) |
| 10 | Roster, parser, `address_lists`, Inbox match | Done |
| 11 | `INBOX/Allowlist` / `INBOX/Blocklist` drain | Done |
| 12 | Admin Domain/User list editors | Done |

**Current product policy (reopenable):**

- Lists override **routing only**. Still scan; **never Bayes-learn** from list hits.
- Scan **From + Sender** only. Reply-To is ignored (spoofable). IMAP drag writes **From only**, never `@host`.
- **User lists** (`actual_name`): addresses **and** `@host` / bare host. **Domain lists** (YAML roster): addresses and `@host`.
- Match stop-on-first-hit: (1) user address (2) user `@host` (3) domain-list address (4) domain-list `@host`. User list overrides the roster domain list. Address beats whole-domain on the same list.
- Allow wins **only** on a true tie at the winning step; `list_conflict` is **audit-only** (events row; routing still allow).
- After IMAP drag: upsert/flip then **MOVE mail back to Inbox**.
- Inbox scan only (not Junk poll). Caps: `max_list_per_run=100`, `max_list_entries=1000`.
- Roster type (`company`/`personal`) is v1 metadata only.
- IMAP drags persist immediately. Dashboard Save is the only batched editor.

**Dashboard UX after slice 12:**

- Save: clear dirty on submit so the browser does not show “Leave site?”
- Find-in-list: keep caret in the search box; highlight **all** matching textarea lines via overlay; clear highlights when query is empty or has no matches
- Messages: click **Score** to sort (SQL); first click high→low, click again toggles
- Learned: click **Event** to sort (SQL); first click A→Z, click again toggles
- `script-src 'self'`; `filter/lists.js` served as `/lists.js`

**Tests:** full `filter/` suite **191 passed** (Docker `python:3.12-slim`). Host has no pytest/`ensurepip`. Use:

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
- **Do not edit the plan file** unless asked (`allow_block_sliced_plan.md` status table may still say “ready to implement”; code is ahead of that table). Allow/block **product policy is reopenable** whenever asked.
- Slices 9–12 **win** over `new_requirements.md`.
- Caddy / Netbird / `spam.bytelord.net` is **out of scope** for these slices.
- Filter talks IMAP `LOGIN` only. OAuth lives in **email-oauth2-proxy**, not this repo.
- Live `accounts.yml` is gitignored; `accounts.yml.example` is the tracked template.
- Dashboard list POST is admin-only + CSRF; login body limit stays 16 KiB; list POST 256 KiB.
- Shadow: no Inbox/Junk/Trash auto-junk; Train-* and Allowlist/Blocklist drain + MOVE-back-to-Inbox are allowed.

---

## What’s next (suggested)

1. **High-score remediation (follow-up)** — Amazon ~25 was mostly `BROKEN_HEADERS` + `BLACKLIST_DMARC` + SPF/DKIM fails on the IMAP path, not Bayes. Next: rspamd local.d weight overrides / disable misleading IMAP auth symbols; fix Spamhaus open-resolver / URIBL blocked. Use `explain_score.py` to confirm before changing weights.
2. **Mode promotion** — Stay in **shadow** until scores look sane; then `flag` then `move`.
3. **Do not** implement generic IMAP user/password for `bytelord.net` unless asked (new auth path).
4. More M365 mailboxes only with Exchange grant + proxy section + YAML.
5. Dashboard: Domain/User lists accept addresses and `@host`; Score column shows top symbols when `score_detail` is present.
6. Deferred from older handoff: container lockdown, Redis LRU, GHA SHA pins, oversize MIME, Entra cert instead of client secret.

---

## Key files for a new agent

| File | Why |
|---|---|
| `IMPLEMENTATION_STATUS.md` | This file |
| `design-arch/allow_block_sliced_plan.md` | List/Bayes product decisions (reopenable) |
| `design-arch/slice9_shared_bayes.md` … `slice12_dashboard_lists.md` | Implementation specs |
| `design-arch/sliced_plan_code_review_fixes.md` | Slices 1–8 + deferred ops |
| `filter/bootstrap_train.py` | Trained-* re-feed (`--all-trained`) |
| `filter/explain_score.py` | Dump rspamd symbols for one IMAP UID |
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
