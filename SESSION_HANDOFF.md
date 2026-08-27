# Session handoff — imap-spamfilter (ByteLord VPS)

**Last updated:** 2026-08-26  
**Repo:** `/opt/bytelord/projects/imap-spamfilter`  
**Remote:** `github.com:bytecave/imap-spamfilter.git` (branch `main`)  
**Upstream fork of:** marcelverdult/imap-spamfilter  

Read this first in a new agent/chat session before exploring the tree.

---

## What this project is

Self-hosted IMAP spam filter: Python filter + Rspamd + Redis + Unbound.
One thread per mailbox; scores via Rspamd; moves/learns from Inbox↔Junk.
Designed for ~16 IMAP accounts on this headless Ubuntu VPS (bytelord).

**OAuth is not in this repo.** Real mailbox auth will go through
`email-oauth2-proxy` (to be cloned as a **sibling** under
`/opt/bytelord/projects/`). This filter will LOGIN with plaintext to the
proxy on localhost/docker network; the proxy speaks XOAUTH2 to providers.

---

## What we accomplished (slices 1–8 + VPS layout)

All eight code-review slices are **implemented and pushed** to `origin/main`
(through `61c05fd`). Local uncommitted work may also include VPS secrets
alignment and project-dir `accounts.yml` — check `git status`.

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

### ByteLord layout (current)

| Path | Role |
|---|---|
| `/opt/bytelord/projects/imap-spamfilter/` | Git checkout + **accounts.yml** (gitignored) |
| `/opt/bytelord/secrets/imap-spamfilter.env` | `RSPAMD_PASSWORD` (required); optional `REDIS_PASSWORD` |
| `/opt/bytelord/data/imap-spamfilter/` | redis data, redis-config, rspamd local.d+data, SQLite **state** |
| `/opt/bytelord/compose/imap-spamfilter/compose.yaml` | Live compose (copy of `deploy/bytelord-compose.yaml`) |

**Secret resolution (filter):** env `RSPAMD_PASSWORD` → mounted `SECRETS_FILE`
→ `$STATE_DIR/controller.password`. Bootstrap on VPS **copies**
`RSPAMD_PASSWORD` from the secrets file into rendered rspamd configs so
filter and rspamd agree.

**accounts.yml:** lives in the **project directory**, mode 640, listed in
`.gitignore`. Passwords are `"Dummy"` until the OAuth proxy is wired.
Compose bind-mounts:
`/opt/bytelord/projects/imap-spamfilter/accounts.yml` → `/app/accounts.yml`.

Workspace rule: use `graphify query` before exploring; `graphify update .`
after code edits. Graph lives in `graphify-out/` (gitignored).

---

## How to deploy (VPS)

```bash
# 1. Secrets already exist; ensure RSPAMD_PASSWORD is set:
#    /opt/bytelord/secrets/imap-spamfilter.env  (prefer mode 600)

# 2. Bootstrap data dirs + render rspamd/redis from secrets:
bash /opt/bytelord/projects/imap-spamfilter/deploy/vps-bootstrap.sh

# 3. Edit accounts (gitignored):
nano /opt/bytelord/projects/imap-spamfilter/accounts.yml

# 4. Start stack:
export SPAMFILTER_UID=$(id -u) SPAMFILTER_GID=$(id -g)
docker compose -f /opt/bytelord/compose/imap-spamfilter/compose.yaml up -d --build

# 5. Logs:
docker compose -f /opt/bytelord/compose/imap-spamfilter/compose.yaml logs -f spamfilter
```

If you change `RSPAMD_PASSWORD`, re-run `vps-bootstrap.sh` then restart.

Dashboard (optional): `127.0.0.1:8080`  
`docker exec -it spamfilter python dashboard.py` to add a user.

After editing `deploy/bytelord-compose.yaml`, copy to
`/opt/bytelord/compose/imap-spamfilter/compose.yaml` (or keep them in sync).

---

## How to test

```bash
cd /opt/bytelord/projects/imap-spamfilter/filter
uv run --with pytest --with-requirements requirements.txt python -m pytest -q
```

Expect ~63+ tests. No system `pip`; use `uv`.

Python via `uv`. Do **not** commit unless asked. Keep slice-style commits
separate if committing mixed work.

---

## Containers: filter vs OAuth proxy

**Use two (or more) containers, not one shared container.**

- A container = one primary process + its filesystem.
- Mixing the Python filter and `email-oauth2-proxy` in one image makes
  upgrades, logs, and restarts painful.
- Correct pattern: **separate containers on the same Docker network**
  (`spamnet` or `bytelord-net`).
  - Filter accounts: `imap_host` = proxy service name (e.g.
    `email-oauth2-proxy`), `tls_mode: none`, `password: "Dummy"`.
  - Proxy listens only on the docker network / `127.0.0.1`, never public.
  - Proxy holds the real OAuth client secrets (its own
    `/opt/bytelord/secrets/...` file).

Planned sibling clone: `/opt/bytelord/projects/email-oauth2-proxy`  
(with compose under `/opt/bytelord/compose/...` when ready).

---

## What's next (new session work)

1. **Commit/push** any remaining local VPS + accounts-path changes if not
   already on `origin/main` (`git status` / `git log -5`).
2. **OAuth project:** clone Simon Robinson `email-oauth2-proxy` as sibling;
   ByteLord compose + secrets; bind to localhost/spamnet only.
3. **Integration:** point `accounts.yml` at the proxy (`tls_mode: none`,
   Dummy password); verify IDLE + token refresh over long runs.
4. **PoC:** one mailbox in `shadow`, then promote to `flag`/`move`.
5. **Do not** put real mailbox passwords in this repo; Dummy + proxy only.

Deferred (see parent plan): container lockdown, Redis LRU, GHA SHA pins,
partial MIME of oversize messages.

---

## Key files for a new agent

| File | Why |
|---|---|
| `design-arch/sliced_plan_code_review_fixes.md` | Overall plan + deferred |
| `design-arch/spamfilter_discussion_and_code_review.md` | OAuth proxy architecture discussion |
| `design-arch/slice4_connection_config.md` | `tls_mode: none` for localhost |
| `deploy/bytelord-compose.yaml` | VPS compose source of truth |
| `deploy/vps-bootstrap.sh` | One-shot bootstrap wrapper |
| `unraid/bootstrap.sh` | Renders rspamd/redis; reads secrets |
| `filter/filter.py` | Engine (`_load_rspamd_password`, `connect_imap`) |
| `accounts.yml` | Runtime accounts (gitignored; create from example) |
| `accounts.yml.example` | Template (tracked) |
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
