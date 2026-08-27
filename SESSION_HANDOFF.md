# Session handoff — imap-spamfilter (ByteLord VPS)

**Last updated:** 2026-08-27  
**Repo:** `/opt/bytelord/projects/imap-spamfilter`  
**Remote:** `github.com:bytecave/imap-spamfilter.git` (branch `main`)  
**Upstream fork of:** marcelverdult/imap-spamfilter  

Read this first in a new agent/chat session before exploring the tree.

---

## What this project is

Self-hosted IMAP spam filter: Python filter + Rspamd + Redis + Unbound.
One thread per mailbox; scores via Rspamd; moves/learns from Inbox↔Junk.
Designed for ~16 IMAP accounts on this headless Ubuntu VPS (bytelord).

**OAuth is not in this repo.** Mailbox auth goes through sibling
`email-oauth2-proxy`. The filter LOGINs with plaintext Dummy to the proxy
on `spamnet`; the proxy speaks XOAUTH2 to providers.

---

## What we accomplished (slices 1–8 + VPS layout + OAuth PoC)

All eight code-review slices are **implemented and pushed** to `origin/main`
(through `61c05fd`). Check `git status` for local uncommitted work.

| Slice | Spec | Summary |
|---|---|---|
| 1 | `design-arch/slice1_hybrid_shadow_mode.md` | Hybrid shadow: no Inbox/Junk/Trash writes; Train-* OK |
| 2 | `design-arch/slice2_imap_fetch_discipline.md` | 5 MiB FETCH cap; Junk watermark |
| 3 | `design-arch/slice3_inbox_bookmark.md` | Bookmark advances only on terminal UID prefix |
| 4 | `design-arch/slice4_connection_config.md` | `tls_mode` / YAML bools / IDLE skip |
| 5 | `design-arch/slice5_message_identity.md` | IMAP UID PK, not Message-ID |
| 6 | `design-arch/slice6_rspamd_scan_metadata.md` | Real From; Rcpt = Bayes; no fake Ip |
| 7 | `design-arch/slice7_ops_secrets_supply_chain.md` | Bootstrap pin, YAML>env retention, image pins |
| 8 | `design-arch/slice8_dashboard_hardening.md` | Dashboard password fallback, CSP, redact_log |

Parent plan: `design-arch/sliced_plan_code_review_fixes.md` (status table = done).

### OAuth PoC (2026-08-27) — working

- `email-oauth2-proxy` on `spamnet`, M365 **app-only** (CCG) for
  `rich@bytecave.net` only.
- Filter in **shadow**: connected, IDLE yes, folder discovery (Junk Email /
  Deleted Items), Train-* created, scoring live (`[shadow] would flag …`).
- Filter image pinned to **Python 3.12** (imapclient 3.1.0 + Python 3.14
  breaks `tls_mode: none` / `IMAP4WithTimeout`).

### ByteLord layout (current)

| Path | Role |
|---|---|
| `/opt/bytelord/projects/imap-spamfilter/` | Git checkout + **accounts.yml** (gitignored) |
| `/opt/bytelord/projects/email-oauth2-proxy/` | Upstream clone + local Dockerfile/deploy overlay |
| `/opt/bytelord/secrets/imap-spamfilter.env` | `RSPAMD_PASSWORD` and `REDIS_PASSWORD` (both required) |
| `/opt/bytelord/secrets/email-oauth2-proxy.config` | Proxy INI (tenant, client_id, client_secret); mode 600 |
| `/opt/bytelord/data/imap-spamfilter/` | redis data, redis-config, rspamd local.d+data, SQLite **state** |
| `/opt/bytelord/data/email-oauth2-proxy/cache/` | proxy token cache + logs |
| `/opt/bytelord/compose/imap-spamfilter/compose.yaml` | Live filter stack (`spamnet` **external**) |
| `/opt/bytelord/compose/email-oauth2-proxy/compose.yaml` | Live proxy (`spamnet` external; `127.0.0.1:1993`) |

**accounts.yml:** project dir, mode 640, gitignored. PoC entry uses
`imap_host: email-oauth2-proxy`, `imap_port: 1993`, `tls_mode: none`,
`allow_insecure_tls: true` (required for Docker DNS names),
`password: "Dummy"`, `mode: shadow`.

**Compose note:** do not use YAML aliases like `*secrets-file:/path:ro`
(go-yaml rejects alias+suffix). Keep secret paths literal in volumes.

Workspace rule: use `graphify query` before exploring; `graphify update .`
after code edits. Graph lives in `graphify-out/` (gitignored).

---

## How to deploy (VPS)

```bash
# Proxy first (secrets already filled):
docker compose -f /opt/bytelord/compose/email-oauth2-proxy/compose.yaml up -d --build

# Filter stack:
export SPAMFILTER_UID=$(id -u) SPAMFILTER_GID=$(id -g)
docker compose -f /opt/bytelord/compose/imap-spamfilter/compose.yaml up -d --build

# Logs:
docker compose -f /opt/bytelord/compose/imap-spamfilter/compose.yaml logs -f spamfilter
docker compose -f /opt/bytelord/compose/email-oauth2-proxy/compose.yaml logs -f
```

If you change `RSPAMD_PASSWORD` or `REDIS_PASSWORD`, re-run
`vps-bootstrap.sh` then restart. Bootstrap renders redis/rspamd configs
from the secrets file only; it does **not** write
`data/imap-spamfilter/state/{controller,redis}.password`.

Dashboard (optional): `127.0.0.1:8080`  
`docker exec -it spamfilter python dashboard.py` to add a user.

---

## How to test

```bash
cd /opt/bytelord/projects/imap-spamfilter/filter
uv run --with pytest --with-requirements requirements.txt python -m pytest -q
```

Expect ~63+ tests. No system `pip`; use `uv`. Do **not** commit unless asked.

---

## Containers: filter vs OAuth proxy

**Two compose stacks, one shared `spamnet`.**

- Filter: `imap_host: email-oauth2-proxy`, `tls_mode: none`,
  `allow_insecure_tls: true`, `password: "Dummy"`.
- Proxy: listen `0.0.0.0:1993` inside container; host publish
  `127.0.0.1:1993` only. Secrets in
  `/opt/bytelord/secrets/email-oauth2-proxy.config`.
- Clone vs fork: keep upstream clone; ByteLord Dockerfile/compose are
  local overlays. `git pull` does not touch `/opt/bytelord/secrets` or
  `/opt/bytelord/compose`.

### M365 Entra (tenant-wide app; mailbox grants one-by-one)

App already registered for ByteCave small-business tenant (domains
bytecave.net, eizenhoefer.net, rjmetalfab.com). PoC mailbox grant:
`rich@bytecave.net` only (`IMAP.AccessAsApp` + Exchange
`New-ServicePrincipal` + `Add-MailboxPermission`).

To add another M365 mailbox later:

1. `Add-MailboxPermission -Identity "user@domain" -User $sp.Identity -AccessRights FullAccess`
2. Add `[user@domain]` CCG block to proxy secrets config (same tenant /
   client_id / secret).
3. Add matching `accounts.yml` entry (`mode: shadow` first).
4. Restart proxy then spamfilter.

Personal Gmail / live.com: deferred (delegated/device auth, not CCG).

---

## What's next

1. Let shadow run; watch IDLE + ~1h token refresh.
2. Commit/push local filter changes when asked (Dockerfile 3.12 pin,
   compose external spamnet / literal secrets path, accounts.yml.example).
3. Add more M365 mailboxes via steps above; then Gmail / live.com.
4. Promote PoC `shadow` → `flag` → `move` once scores look sane.

Deferred (see parent plan): container lockdown, Redis LRU, GHA SHA pins,
partial MIME of oversize messages; later prefer Entra certificate auth
over client secret.

---

## Key files for a new agent

| File | Why |
|---|---|
| `design-arch/sliced_plan_code_review_fixes.md` | Overall plan + deferred |
| `design-arch/spamfilter_discussion_and_code_review.md` | OAuth proxy architecture discussion |
| `design-arch/slice4_connection_config.md` | `tls_mode: none` + `allow_insecure_tls` |
| `deploy/bytelord-compose.yaml` | Filter VPS compose source of truth |
| `../email-oauth2-proxy/deploy/bytelord-compose.yaml` | Proxy compose source of truth |
| `../email-oauth2-proxy/emailproxy.config.example` | Proxy INI template (no secrets) |
| `filter/Dockerfile` | Python 3.12 pin for plaintext IMAP |
| `accounts.yml` | Runtime accounts (gitignored) |
| `accounts.yml.example` | Template with proxy pattern |
| `.cursor/rules/graphify.mdc` | Must use graphify before explore |

---

## Graphify

```bash
cd /opt/bytelord/projects/imap-spamfilter
graphify query "<architecture question>"
# after code edits:
graphify update .
```

---

## Commit / push note

User preference: commit when asked; separate commits per logical slice.
Python tests via `uv` as above. Never commit `/opt/bytelord/secrets/*` or
`accounts.yml` with real credentials (Dummy is fine but still gitignored).
