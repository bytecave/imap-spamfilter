# Session handoff — imap-spamfilter (ByteLord VPS)

**Last updated:** 2026-09-24  
**Repo:** `/opt/bytelord/projects/imap-spamfilter`  
**Remote:** `github.com:bytecave/imap-spamfilter.git` (branch `main`)  
**Upstream fork of:** marcelverdult/imap-spamfilter  

---

## Mandatory before doing anything else

A new agent **must** do all three before exploring code or proposing fixes:

1. **Read this file** (where we left off + next steps).
2. **Read [`IMPLEMENTATION_STATUS.md`](IMPLEMENTATION_STATUS.md) in full** — architecture, policy, live VPS, remediation, What’s next. This handoff is the short continuity note; that file is the durable map. If they disagree, **trust `IMPLEMENTATION_STATUS.md` for product facts** and this file for “continue here.”
3. **Search Supermemory** (MCP `plugin-cursor-supermemory-supermemory`, `container=project`) before answering “why / what next / how scoring works.” Do not rely on chat memory. Useful seeds:
   - `IMAP-path remediation buckets A B C mx.microsoft.com`
   - `Bayes retrain bytelord Delivered-To Rcpt`
   - `dashboard Trained rescore score_detail`
   - `imap-spamfilter shadow dashboard 8099`
   Also `supermemory_list` (recent project memories). After decisions or live deploys, `supermemory_add` with `container=project`.

Also read `/home/bytecave/.claude/CLAUDE.md` (Cursor user rule) and use Agent Mail + graphify as that file and `IMPLEMENTATION_STATUS.md` § Agent onboarding require.

---

## Where we left off (2026-09-24 evening)

IMAP-path false positives (buckets A+B+C) are **fixed and live**. Shared Bayes was **wiped and rebuilt**. Dashboard **Trained-*** rows were **rescored** so Messages shows current engine scores, not first-scan scores. Accounts remain **`mode: shadow`**.

Operator review of sample mail (Spambrella, Chase, BOA, Randy, Vancouver Bolt, Chelsea, iPic, 5Tool) showed:

- High stored scores with `BROKEN_HEADERS` / `HFILTER_*` were **old first-scan rows**. Rescored with current code they drop to ham territory when auth is clean.
- **Auth-passed content spam** (cold pitch / promo) can still score low: 5Tool −1.10, Chelsea ~0 after Bayes, iPic ~+6 from `MICROSOFT_SPAM`. Operator will Train-Spam those going forward.
- Chelsea was mistakenly in Trained-Ham; removed to Trained-Spam and Bayes flipped to spam (bobbi_rjmetalfab UID 15).

### Scoring stack (do not re-diagnose)

| Layer | Status |
|---|---|
| **A** | `HFILTER_HOSTNAME_UNKNOWN` / `RDNS_NONE` weight 0 in live `rspamd/local.d/hfilter_group.conf` |
| **B** | `apply_m365_auth_trust` suppresses DKIM/SPF/DMARC/`BLACKLIST_DMARC` only from outermost Microsoft AR (`mx.microsoft.com` **or** `compauth=` + `Received-SPF` receiver `protection.outlook.com`) |
| **C** | Bare `bayes_user` (`bytelord`) is **not** HTTP `Rcpt`; scan prepends `Delivered-To: bytelord` and uses the mailbox as `Rcpt`. Real broken MIME still scores `BROKEN_HEADERS` +8 |
| **Bayes** | Shared notebook `bytelord`; rebuilt 2026-09-24 (snapshot `redis/dump.rdb.bak-20260924`) |
| **Filter vs rspamd actions** | Filter uses `accounts.yml` `threshold`. Cosmetic rspamd lines in `actions.conf`: greylist 4, add_header 6, reject 15 |

### Dashboard vs live IMAP

Messages tab reads SQLite `our_score` / `score_detail`. Inbox and top-level Junk were **not** bulk-rescored (operator request). Trained-* **were** rescored into the DB (~2897 stored; ~8 oversize; some empty rspamd stubs repaired). Named Inbox senders vancouverbolt.com and randyhatley1@gmail.com were also updated so those rows show current scores.

---

## Bayes retrain (2026-09-24)

- Stopped `spamfilter` only; left rspamd/redis/unbound up. Never `compose down` redis.
- Deleted only `RSbytelord*` / `learned_ids` / occurrence keys; left fuzzy (`rgb*`/`rgm*`) and neural (`rn_*`).
- Trained-Spam rescored empty-Bayes: score **&lt; 6** → MOVE to Trained-Ham (**981** moved, **207** kept). Then `bootstrap_train.py --all-trained`.
- Redis after re-feed: **learns_spam≈205**, **learns_ham≈2410** (plus 20 Google + 20 Amazon clear-ham learns from newest Inbox mail).
- After restart checks: payroll 140596 **2.10**; Amazon 235843 **−2.05**; kept VSP spam **15.09**.

---

## Next steps (do these, in order)

1. **Stay in shadow.** Watch dashboard / Train-Spam on content spam (5Tool-style cold pitch, Chelsea, iPic). Promote `flag` → `move` only when the operator asks.
2. **Do not** wipe Bayes again unless the operator asks; operator will teach spam via Train-Spam.
3. Later: full code/security review; CR-016 supply chain; more mailboxes only when asked.
4. Optional later: bulk-rescore Inbox/Junk dashboard rows (not done; operator excluded them).

---

## Project in one breath (verify details in IMPLEMENTATION_STATUS.md)

Self-hosted IMAP spam filter: Python (`filter/filter.py`) + Rspamd **4.2.0** + Redis Bayes + Unbound, Docker network **`spamnet`**. Sibling **`email-oauth2-proxy`** does XOAUTH2 to M365; this filter uses plain IMAP `LOGIN`. Shared Bayes user **`bytelord`**. **List hits are scored** then override routing. Allow drag → ham + Inbox; block drag → spam + Junk. Provider Junk is scored, **not** learned as spam; rescue only in `move` mode. Dashboard `https://spam.bytelord.net` (loopback **8099**); Rspamd WebUI link `https://spam.bytelord.net/rspamd/` when `RSPAMD_WEBUI_URL` is set.

---

## Paths and deploy gotcha

| Path | Role |
|---|---|
| `/opt/bytelord/projects/imap-spamfilter/` | Git checkout; `accounts.yml` gitignored |
| `deploy/bytelord-compose.yaml` | Compose **source of truth** |
| `/opt/bytelord/compose/imap-spamfilter/compose.yaml` | **Live** compose — **does not auto-sync**; diff + `cp` before pull/recreate |
| `/opt/bytelord/data/imap-spamfilter/state/` | SQLite `spamfilter.db` (0700/0600); retrain reports `bayes-retrain-20260924.tsv`, `google-amazon-20260924.tsv` |
| `/opt/bytelord/data/imap-spamfilter/redis/dump.rdb.bak-20260924` | Pre-wipe Redis snapshot |
| `/opt/bytelord/secrets/imap-spamfilter.env` | Secrets — never commit |
| `/opt/bytelord/projects/email-oauth2-proxy/` | OAuth/M365 bridge |

Recreate filter with `SPAMFILTER_UID=1001 SPAMFILTER_GID=1001`. **Do not** `compose down` redis (Bayes). Tests: Docker `python:3.12-slim` only.

---

## Key files

| File | Why |
|---|---|
| `IMPLEMENTATION_STATUS.md` | **Mandatory** durable map |
| `SESSION_HANDOFF.md` | This file |
| `filter/filter.py` | Scan/learn; `rspamd_scan_detail`; `apply_m365_auth_trust` |
| `filter/explain_score.py` | Re-score one UID and print symbols |
| `filter/bootstrap_train.py` | `--all-trained` Trained-* re-feed |
| `filter/dashboard.py` | Messages “why” from `score_detail` |
| `rspamd/local.d/` | `hfilter_group.conf`, `actions.conf` (cosmetic 4/6/15) |
| `README.md` | Operator docs (IMAP-path limitation section) |
| `design-arch/slice6_rspamd_scan_metadata.md` | Locked: no fake `Ip`/`Helo` |

---

## Agent protocol (short)

- Agent Mail project key: `/opt/bytelord/projects/imap-spamfilter`. Reserve files before edits; only the main session commits.
- Graphify before broad explore: `graphify explain` / `path`, or `graphify query "…" --dfs --budget 3333`. `graphify update .` after doc/code batches. Project `.cursor/rules/graphify.mdc` was **removed** (2026-09-23, commit `15cc5f8`); mandates live in `~/.claude/CLAUDE.md` and `~/.cursor/rules/`.
- Commit/push **only when asked**. No secrets, no force-push, no `--no-verify`.
