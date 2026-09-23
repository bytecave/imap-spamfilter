# Session handoff — imap-spamfilter (ByteLord VPS)

**Last updated:** 2026-09-23  
**Repo:** `/opt/bytelord/projects/imap-spamfilter`  
**Remote:** `github.com:bytecave/imap-spamfilter.git` (branch `main`)  
**Upstream fork of:** marcelverdult/imap-spamfilter  

---

## Mandatory before doing anything else

A new agent **must** do all three before exploring code or proposing fixes:

1. **Read this file** (where we left off + next steps).
2. **Read [`IMPLEMENTATION_STATUS.md`](IMPLEMENTATION_STATUS.md) in full** — architecture, policy, live VPS, 2026-09-23 findings, What’s next. This handoff is the short continuity note; that file is the durable map. If they disagree, **trust `IMPLEMENTATION_STATUS.md` for product facts** and this file for “continue here.”
3. **Search Supermemory** (MCP `plugin-cursor-supermemory-supermemory`, `container=project`) before answering “why / what next / how scoring works.” Do not rely on chat memory. Useful seeds:
   - `IMAP-path remediation buckets A B C mx.microsoft.com`
   - `BROKEN_HEADERS MIME_TRACE pending investigation`
   - `Amazon UID 235843 DKIM body hash`
   - `imap-spamfilter shadow dashboard 8099`
   Also `supermemory_list` (recent project memories). After decisions or live deploys, `supermemory_add` with `container=project`.

Also read `/home/bytecave/.claude/CLAUDE.md` (Cursor user rule) and use Agent Mail + graphify as that file and `IMPLEMENTATION_STATUS.md` § Agent onboarding require.

---

## Where we left off (2026-09-23)

Conversation continued from the Rspamd **4.2.0** deploy and dashboard WebUI link (done, pushed 2026-09-22) into **why legit mail scores as spam**.

Operator showed a Google Security alert (`no-reply@accounts.google.com`, rich_bytecave, **+18.50**, `shadow`, Inbox) with `BROKEN_HEADERS=+8`, `HFILTER_HOSTNAME_UNKNOWN=+2.5`, `DMARC_POLICY_REJECT=+2`. Same pattern on Amazon (example: Inbox UID **235843**, `auto-confirm@amazon.com`, score **~21.95**).

**Root cause (verified on the live mailbox, not assumed):**

- Proofpoint / M365 score at **SMTP ingress** and stamp `Authentication-Results: mx.microsoft.com` with `spf=pass`, `dkim=pass`, `dmarc=pass`.
- This filter **IMAP-fetches the stored copy** and posts it to rspamd `/checkv2` with **no SMTP `Ip`/`Helo`** (`filter/filter.py` `rspamd_scan_detail`).
- Amazon sample: stored body SHA-256 **does not match** the DKIM `bh=` Microsoft already verified. Stripping `X-MS-*` / ARC / Authentication-Results **does not** clear the fails. So this is **not** “M365 headers are broken.”
- `BROKEN_HEADERS` is rspamd’s MIME-parser flag (`task:has_flag('broken_headers')`), **not** “bad Microsoft headers.” Amazon `MIME_TRACE` showed part `2:~`. **Do not globally disable it yet** — investigation is pending (bucket C).

**Locked direction (do not blanket-downweight auth):**

| Bucket | What | Action |
|---|---|---|
| **A** | `HFILTER_HOSTNAME_UNKNOWN`, `RDNS_NONE` | Safe to zero. No client IP → ham and spam pay the same. |
| **B** | `R_DKIM_REJECT`, `R_SPF_FAIL`, `DMARC_POLICY_*`, `BLACKLIST_DMARC` | Suppress failure weight **only when** trusted AR authserv-id **`mx.microsoft.com`** says that check **pass**. Fail / missing / other authserv → **keep** the symbol. Prefer rspamd `trusted_authserv_id` and ARC `whitelisted_signers_map` (`microsoft.com`) + `adjust_dmarc`. |
| **C** | Bayes, URLs, fuzzy, neural, lists, **`BROKEN_HEADERS`** | **Keep.** Investigate `BROKEN_HEADERS` separately before any exception. |

**Trust model:** Do **not** trust Microsoft junk/SCL (why this project exists). **Do** trust edge SPF/DKIM/DMARC from `mx.microsoft.com`. Spammers cannot make that real check return pass without actually passing auth. Residual risk is implementation (only honor that authserv-id; watch duplicate AR headers) plus spam that **correctly** passes auth (BEC) — content scoring must still catch that.

Ham training **cannot** cancel these auth/header symbols. **All accounts stay `mode: shadow`.** Leaving shadow before A+B would auto-Junk a lot of good mail.

**Not committed yet** as of this handoff write: updates to this file and `IMPLEMENTATION_STATUS.md` (2026-09-23 findings). Ask before commit/push.

---

## Next steps (do these, in order)

Full wording and evidence live in `IMPLEMENTATION_STATUS.md` → “2026-09-23 findings” and “What’s next.”

1. **Implement buckets A+B** in `rspamd/local.d` (small filter helper only if rspamd config cannot express B). Validate with `docker exec spamfilter python explain_score.py …` and Messages `score_detail` on:
   - Google Security alert (rich_bytecave Inbox UID **235853** is one sample)
   - Amazon UID **235843**
   - known **auth-fail** spam (must still score high)
   - if available, auth-pass phishing/BEC (content symbols must still fire)
   Success: legit samples drop under the reject threshold (~15) without a free pass for auth-fail spam.
2. **Investigate bucket C `BROKEN_HEADERS`** on those same samples (`MIME_TRACE`, MIME structure). Decide narrow exception vs leave scored. **Do not** zero it in the same change as A+B without that investigation.
3. **Stay in shadow** until A+B are live and dashboard scores look sane. Promote `flag` → `move` only when the operator asks.
4. Later: full code/security review (Claude Code or Codex); CR-016 supply chain; more mailboxes only when asked.

---

## Project in one breath (verify details in IMPLEMENTATION_STATUS.md)

Self-hosted IMAP spam filter: Python (`filter/filter.py`) + Rspamd **4.2.0** + Redis Bayes + Unbound, Docker network **`spamnet`**. Sibling **`email-oauth2-proxy`** does XOAUTH2 to M365; this filter uses plain IMAP `LOGIN`. Shared Bayes user **`bytelord`**. **List hits are scored** then override routing (older docs that say list hits skip `/checkv2` are stale). Allow drag → ham + Inbox; block drag → spam + Junk. Provider Junk is scored, **not** learned as spam; rescue only in `move` mode. Dashboard `https://spam.bytelord.net` (loopback **8099**); Rspamd WebUI link `https://spam.bytelord.net/rspamd/` when `RSPAMD_WEBUI_URL` is set.

---

## Paths and deploy gotcha

| Path | Role |
|---|---|
| `/opt/bytelord/projects/imap-spamfilter/` | Git checkout; `accounts.yml` gitignored |
| `deploy/bytelord-compose.yaml` | Compose **source of truth** |
| `/opt/bytelord/compose/imap-spamfilter/compose.yaml` | **Live** compose — **does not auto-sync**; diff + `cp` before pull/recreate |
| `/opt/bytelord/data/imap-spamfilter/state/` | SQLite `spamfilter.db` (0700/0600) |
| `/opt/bytelord/secrets/imap-spamfilter.env` | Secrets — never commit |
| `/opt/bytelord/projects/email-oauth2-proxy/` | OAuth/M365 bridge |

Recreate filter with `SPAMFILTER_UID=1001 SPAMFILTER_GID=1001`. **Do not** `compose down` redis (Bayes). Tests: Docker `python:3.12-slim` only (last full run **337 passed**, 2026-09-22).

---

## Key files

| File | Why |
|---|---|
| `IMPLEMENTATION_STATUS.md` | **Mandatory** durable map |
| `SESSION_HANDOFF.md` | This file |
| `filter/filter.py` | Scan/learn; `rspamd_scan_detail` |
| `filter/explain_score.py` | Re-score one UID and print symbols |
| `filter/dashboard.py` | Messages “why” from `score_detail` |
| `rspamd/local.d/` | Where A+B config changes go (`actions.conf` reject threshold 15) |
| `README.md` | Operator docs (IMAP-path limitation section) |
| `design-arch/slice6_rspamd_scan_metadata.md` | Locked: no fake `Ip`/`Helo` |

---

## Agent protocol (short)

- Agent Mail project key: `/opt/bytelord/projects/imap-spamfilter`. Reserve files before edits; only the main session commits.
- Graphify before broad explore: `graphify explain` / `path`, or `graphify query "…" --dfs --budget 3333`. `graphify update .` after code edits. Project `.cursor/rules/graphify.mdc` was **removed** (2026-09-23, commit `15cc5f8`); mandates live in `~/.claude/CLAUDE.md` and `~/.cursor/rules/`.
- Commit/push **only when asked**. No secrets, no force-push, no `--no-verify`.
