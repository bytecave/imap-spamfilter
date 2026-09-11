# Session handoff — imap-spamfilter (ByteLord VPS)

**Last updated:** 2026-09-10  
**Repo:** `/opt/bytelord/projects/imap-spamfilter`  
**Remote:** `github.com:bytecave/imap-spamfilter.git` (branch `main`)  
**Upstream fork of:** marcelverdult/imap-spamfilter  

Read **this file** for “where we left off,” then [`IMPLEMENTATION_STATUS.md`](IMPLEMENTATION_STATUS.md) for durable product/deploy detail.

---

## Where we left off (2026-09-10)

Reviewed every finding in [`CHATGPT_CODE_REVIEW.md`](CHATGPT_CODE_REVIEW.md) (**IMAP-CR-001…019**). Implemented valid fixes + tests; appended **Code Review Disposition**. **IMAP-CR-002** ignored (operator). **IMAP-CR-016** accepted risk.

Live `spamfilter` container was **rebuilt and force-recreated** from local `filter/` (includes CR-011 private state modes). Container was **healthy**; 10 accounts connecting in **shadow**. State dir verified `0700`, DB/WAL/SHM `0600`.

**Working tree is dirty and uncommitted.** User has not asked to commit or push.

---

## Must-know for the next agent

1. **Do not discard unrelated untracked files:** `CHATGPT_CODE_REVIEW.md`, `imap-spamfilter-plans-2026-09-08/`, `.gitattributes`, `deploy/fix-cursor-apparmor.txt`, `filter/test_code_review_findings.py`.
2. **Commit/push only if the user asks.** Prefer including disposition doc + new tests with the CR code.
3. **All live accounts remain `mode: shadow`.** Promote to `flag`/`move` only when asked and scores look sane.
4. **Deploy recipe that was just used:**

```bash
export SPAMFILTER_UID=1001 SPAMFILTER_GID=1001
docker compose -f /opt/bytelord/compose/imap-spamfilter/compose.yaml build spamfilter
docker compose -f /opt/bytelord/compose/imap-spamfilter/compose.yaml up -d --force-recreate --no-deps spamfilter
# verify:
docker ps --filter name=^spamfilter$
ls -la /opt/bytelord/data/imap-spamfilter/state
```

5. **Tests (host has no system pytest; use Docker):**

```bash
docker run --rm -v /opt/bytelord/projects/imap-spamfilter:/src -w /src/filter \
  python:3.12-slim bash -c \
  "pip install -q -r requirements.txt pytest==8.4.2 && python -m pytest -q --tb=short"
# Last result: 286 passed
```

6. **graphify:** query before large explores; `graphify update .` after more code edits (graph may be stale vs CR work).

---

## Open / next steps (priority)

| Priority | Item |
|---|---|
| When asked | Commit + push CR working-tree changes |
| Product | Stay shadow; later `flag` → `move` |
| Scoring | IMAP-path symbol weight remediation (`BROKEN_HEADERS` / DMARC / SPF after M365 rewrite); use `explain_score.py` |
| Nice-to-have | CR-016 supply-chain pins (lockfile/hashes, image digests, GHA commit SHAs) |
| Out of scope unless asked | `bytelord.net` generic IMAP auth; Caddy/Netbird public dashboard |

Disposition “limitations” (fingerprint caps, historical event subjects, SQLite CHECKs, fixed 20m heartbeat stale, etc.) are polish — not release blockers.

---

## Project in one breath

Self-hosted IMAP spam filter (Python + Rspamd + Redis + Unbound). Per-mailbox threads; allow/block lists skip `/checkv2`; shared Bayes user `bytelord`. OAuth is **not** in this repo — sibling `email-oauth2-proxy` on shared Docker network `spamnet`.

---

## Paths

| Path | Role |
|---|---|
| `/opt/bytelord/projects/imap-spamfilter/` | Checkout + gitignored `accounts.yml` |
| `/opt/bytelord/compose/imap-spamfilter/compose.yaml` | Live filter stack |
| `/opt/bytelord/data/imap-spamfilter/state/` | SQLite + heartbeat (now private modes) |
| `/opt/bytelord/secrets/imap-spamfilter.env` | Rspamd/Redis passwords |
| `/opt/bytelord/projects/email-oauth2-proxy/` | Proxy project |
| `/opt/bytelord/compose/email-oauth2-proxy/compose.yaml` | Live proxy |
| Dashboard | `127.0.0.1:8080` (SSH tunnel from Windows) |

---

## Key files

| File | Why |
|---|---|
| `CHATGPT_CODE_REVIEW.md` | Findings + disposition (source of truth for CR work) |
| `IMPLEMENTATION_STATUS.md` | Durable status + account table + policy |
| `filter/filter.py` | Core filter (CR-001/003/004/011/017/018, etc.) |
| `filter/dashboard.py` | Dashboard CR fixes |
| `filter/test_code_review_findings.py` | New CR regression suite |
| `design-arch/allow_block_sliced_plan.md` | List/Bayes policy (reopenable) |
| `deploy/bytelord-compose.yaml` | Compose source of truth |

---

## Commit / push note

User preference: commit when asked; never force-push; never skip hooks; never commit secrets or live `accounts.yml`.
