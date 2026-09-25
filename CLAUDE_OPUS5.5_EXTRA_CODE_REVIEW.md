# Claude Opus 5.5 — Extra Code Review: imap-spamfilter

- **Date:** 2026-09-25
- **Reviewed revision:** `ff9461e` ("Document Bayes retrain, Trained-* rescore, and shadow next steps."), identical on `main` and `claude/blissful-dijkstra-qzxvln` at review time.
- **Scope:** every source, config and YAML file under `imap-spamfilter/` (`filter/*.py`, `filter/lists.js`, `filter/Dockerfile`, `filter/requirements.txt`, both Compose files, `deploy/*`, `rspamd/local.d/*`, `redis/*`, `.github/workflows/build.yml`, `accounts.yml.example`, `.env.example`), plus `unraid/bootstrap.sh`, because `deploy/vps-bootstrap.sh` runs it on the VPS.
- **Context read first, oldest to newest:** upstream `marcelverdult/imap-spamfilter` README (Unraid parts ignored), all 27 documents in `imap-spamfilter-plans-2026-09-08/00-chronological/`, `CHATGPT_CODE_REVIEW.md` (including its disposition), `new_requirements.md`, `README.md`, `IMPLEMENTATION_STATUS.md`, `SESSION_HANDOFF.md`, and the commit messages after 2026-09-06. The design docs under `design-arch/` match the archive copies; the only difference is the Rspamd pin in slice 7.
- **Operating assumptions (as instructed):** the service runs only on the private NetBird network. I do **not** report the lack of OAuth2 or the simple dashboard password model as security flaws. Microsoft 365 OAuth stays in `email-oauth2-proxy`. The Unraid templates are out of scope. `deploy/fix-cursor-apparmor.sh` is also out of scope, per the operator's annotation on ChatGPT finding IMAP-CR-002.

## How I verified findings

- **Baseline tests:** `cd filter && python -m pytest -q` on Python 3.12 with the pinned requirements gave **349 passed**.
- **Branch coverage (non-test modules):** `filter.py` 72%, `dashboard.py` 79%, `bootstrap_train.py` 86%, `explain_score.py` 79%. Some core paths have **no coverage** (see OPUS-CR-015).
- **Reproduced with scripts** against the real code and the existing test fakes: OPUS-CR-001, 002, 005 (a real waitress server), 007 and 008.
- **Checked against upstream source, not memory:**
  - Waitress 3.0.2 `parser.py` enforces `max_request_body_size` before WSGI.
  - Rspamd 4.2.0 `mime_headers.c` sets `task->deliver_to` from the first MIME `Delivered-To:`, and `task.c` gives `deliver_to` priority over `Rcpt` as the Bayes principal recipient.
  - Rspamd 4.2.0 `plugins/lua/neural.lua` autotrains by default (`autotrain = true`) from Rspamd's own score in a postfilter.
  - `imapclient` 3.1.0 `move()` requires the MOVE capability, and `expunge(uids)` is UID EXPUNGE.
- **Labels:** "Verified" means reproduced, or proven from source. "Plausible" means the code path is real but the trigger depends on server behavior that I could not observe from here.

## Severity guide

- **High:** can lose mail, silently stop filtering, defeat a stated safety rail, or corrupt the shared classifier.
- **Medium:** breaks a documented contract, loses training feedback, reverses user intent in `move`/`flag` mode, or causes avoidable IMAP/DB instability.
- **Low:** diagnostics, UX, docs, or defense in depth.

## Findings index

| ID | Sev | Area | File(s) | Title | Planned disposition |
|---|---|---|---|---|---|
| OPUS-CR-001 | High | List drain / data safety | `filter/filter.py:4320-4352, 4458-4500` | Allowlist/Blocklist de-dup UID-EXPUNGEs the dragged message on a Message-ID collision | Fix |
| OPUS-CR-002 | High | Scan availability | `filter/filter.py:3408-3411, 3490-3495, 3893-3896, 3984-3990` | One poison message halts Inbox scanning (and Junk learning) for that account forever | Fix |
| OPUS-CR-003 | High | Security / rescue | `filter/filter.py:3726-3840, 3991-4018` | Provider-Junk rescue ignores Microsoft's spoof verdict; allow hits come from spoofable From/Sender | Fix (rescue guard) + recommend policy |
| OPUS-CR-004 | High | Classifier integrity | `rspamd/local.d/neural.conf`, `filter/filter.py:416-450` | Rspamd neural autotrains on unadjusted scores and on every re-scan; old `rn_*` keys were kept | Recommend (operator decision) |
| OPUS-CR-005 | Medium | Dashboard contract | `filter/dashboard.py:2078-2084`, `filter/test_dashboard.py:365-386` | Waitress 16 KiB cap rejects list saves of about 600+ entries (413), against slice 12's 256 KiB | Fix |
| OPUS-CR-006 | Medium | Bayes identity | `filter/filter.py:2338-2344` | Scan-time Bayes key can come from the message's own `Delivered-To`, unlike learning | Fix |
| OPUS-CR-007 | Medium | Learning loss | `filter/filter.py:3235-3243, 3914-3916` | User re-junking a filter-rescued message is never learned as spam | Fix |
| OPUS-CR-008 | Medium | Move-mode UX | `filter/filter.py:3971-4018, 3726-3748` | Rescue reverses the user's own moves of old or non-Inbox mail into Junk | Fix |
| OPUS-CR-009 | Medium | Retention safety | `filter/filter.py:4531-4592` | Junk retention can trash unseen, pending-learn, or allowlisted provider-Junk (flag/move) | Fix |
| OPUS-CR-010 | Medium | Filter bypass | `filter/filter.py:3414-3418, 3899-3901` | Mail without a Message-ID is never scored, list-matched, moved, or learned | Fix |
| OPUS-CR-011 | Medium | SQLite concurrency | `filter/filter.py:1605-1613` | Deferred `BEGIN` read-then-write transactions can fail with `SQLITE_BUSY` | Fix |
| OPUS-CR-012 | Medium | Reconnect/backoff | `filter/filter.py:4614-4737` | Backoff resets on connect, so a repeatable in-loop failure re-logs in every ~5 s | Fix |
| OPUS-CR-013 | Medium | IMAP load | `filter/filter.py:4129-4218, 4028-4126` | Learn rate budget is checked after downloading full bodies | Fix |
| OPUS-CR-014 | Medium | MOVE semantics | `filter/filter.py:4200-4217, 3701-3723` | MOVE-as-COPY leftovers are handled only for list folders | Fix (Train-* guard) + verify live (Plausible) |
| OPUS-CR-015 | Medium | Tests | `filter/test_*.py` | Core learning and move-mode executors have no tests | Add tests with each fix; recommend the rest |
| OPUS-CR-016 | Low | Audit noise | `filter/filter.py:3500-3513, 3676-3687` | `pending_move_canceled` is logged for every allowlisted message | Fix |
| OPUS-CR-017 | Low | Secrets on host | `unraid/bootstrap.sh:143-167, 39` | Rendered secret configs are written under the default umask; stale fallback version | Fix |
| OPUS-CR-018 | Low | Metrics | `filter/dashboard.py:1422-1506` | "Routing catch rate" double-counts list hits and counts provider-Junk scans | Recommend |
| OPUS-CR-019 | Low | Docs vs intent | `filter/filter.py:3182`, README, STATUS | HTTP `Rcpt` is the first To/Cc address, not "the mailbox" as documented | Recommend (operator decision) |
| OPUS-CR-020 | Low | Duplicate learning | `filter/filter.py:3430-3471, 3917-3969` | Allow/Block drags learn twice (drain, then revert / user-move path) | Recommend |
| OPUS-CR-021 | Low | Input validation | `filter/filter.py:709-735` | The list parser accepts patterns that can never match | Recommend |
| OPUS-CR-022 | Low | Deploy config | `deploy/bytelord-compose.yaml:77-91` | TZ=Europe/Berlin, `REDIS_PASSWORD` in the filter env, no `stop_grace_period` | Recommend (ops) |
| OPUS-CR-023 | Low | Dashboard throttle | `filter/dashboard.py:133-138, 373-398` | Every Caddy-proxied client throttles as the Docker bridge IP | Recommend (ops) |
| OPUS-CR-024 | Low | Diagnostics | `filter/filter.py:130-141` | `redact_log` deletes all text after the word "login" | Recommend |
| OPUS-CR-025 | Low | Config parsing | `filter/dashboard.py:246-248` | `DASHBOARD_USERS` splits on commas, which is also a scope separator | Recommend |
| OPUS-CR-026 | Low | UI | `filter/lists.js:28-33, 126-130` | After a 400, Cancel restores the rejected text, not the saved list | Recommend |
| OPUS-CR-027 | Low | Maintainability | `filter/filter.py:1418-1587, 4024-4025, 4662`; `filter/dashboard.py:1906` | Unpruned `_dashboard` events, non-atomic legacy migration, duplicate rescue call | Recommend |
| OPUS-CR-028 | Low | Docs drift | `README.md`, `design-arch/*` | Stale folder count, status tables, and "skip /checkv2" statements | Recommend |
| OPUS-CR-029 | Medium | Scoring config | `rspamd/local.d/fuzzy_check.conf` | Invalid fuzzy `encryption_key` made rspamd drop the rspamd.com fuzzy rule; fuzzy never scored (found during deploy) | Fixed |

**Counts:** 4 High, 11 Medium, 13 Low.

---

## High

### OPUS-CR-001 — The list-drain de-dup can delete the dragged message on a Message-ID collision

**Severity:** High · **Status:** Verified (reproduced)
**Files:** `filter/filter.py` `_message_ids_in_folder` (4320-4352) and `_drain_list_folder` (4458-4500); `filter/test_list_folders.py:216-232`

**What happens.** After a drag into `INBOX/Allowlist` or `INBOX/Blocklist`, the drain searches the destination (Inbox or Junk) for `HEADER Message-ID <x>`. On any hit it puts the dragged UID in `to_clear` and calls `_expunge_source_uids()`, which sets `\Deleted` and runs UID EXPUNGE, **without moving it**. Message-ID is sender-controlled and not unique. Mailing-list duplicates, broken mailers that reuse IDs, and spam campaigns all produce collisions.

**Failure scenario (reproduced).** Microsoft junks a legitimate vendor mail A (`Message-ID: <dup@x.com>`). The user drags A from Junk to Allowlist. Inbox already holds a *different* message B with the same Message-ID. Result: the person-allow row is written, then A is **expunged and never moved**. A is gone from Junk (the user moved it) and from the Allowlist (expunged). My reproduction printed `moved: [] expunged: [[1]]`.

**Trace**

- **Requirement:** README hard rule "Never deletes"; `new_requirements.md` §9.3 "Dragging a message ... does not lose the message"; slice 11 §4 "MOVE the message back to Inbox so mail is not lost".
- **Architecture:** slice 5 (`07__slice5_message_identity`): identity is `(account, folder, uidvalidity, uid)`; Message-ID is metadata only.
- **Acceptance:** slice 11 §8 "Caps enforced without trapping mail"; the 2026-09-21 change added the EXPUNGE exception only for leftover copies after MOVE-as-COPY.
- **Implementation:** a Message-ID match is treated as proof that the same message already sits in the destination.
- **Tests:** `test_drain_allowlist_expunges_without_move_when_already_in_inbox` pins the Message-ID-only behavior. No test covers a collision with different bytes.

**Recommendation.** Before expunging, prove the destination holds a **byte-identical** copy: fetch the Message-ID candidates (under the 5 MiB cap) and compare SHA-256 with the dragged message. If nothing matches, or the check can't run, MOVE. A duplicate is recoverable; an expunge is not.

**Tests to add:** (1) same Message-ID, different bytes in the destination → MOVE, no EXPUNGE; (2) byte-identical copy in the destination → EXPUNGE only (keep today's leftover cleanup); (3) destination candidate is oversize → MOVE.

### OPUS-CR-002 — One poison message halts scanning for its account forever

**Severity:** High · **Status:** Verified (reproduced)
**Files:** `filter/filter.py` `_scan_inbox_uid_batch` (3408-3411, 3490-3495) and `poll_junk` (3893-3896, 3984-3990)

**What happens.** Slice 3 made every rspamd failure and every empty body non-terminal: the pass stops and the bookmark stays put. That is correct for a short outage, but nothing separates an outage from a message that fails **every time**. Examples:

- rspamd returns non-JSON or an error for one malformed message;
- the score falls outside `±reject_score_above`;
- the server keeps returning an empty `BODY[]` for one item.

Each pass retries the same UID, fails, and stops. No later UID in that account is ever scored or acted on.

In `poll_junk` the same halt also blocks **user Inbox→Junk learning** for every later Junk UID. Since 2026-09-21 provider Junk is scored, so a scan failure halts the loop before any later user moves are handled.

**Failure scenario (reproduced).** UIDs 1, 2, 3, where rspamd always rejects UID 2. After 20 passes the bookmark is still 1 and UID 3 has never been scored. Filtering stops quietly for that mailbox. The only signals are recurring `scan_failed` events and a `scan_fail_streak` log at 1, 10, 50, 250… failures.

**Trace**

- **Requirement:** README architecture: every account is scanned and moved.
- **Architecture:** slice 3 §4: "rspamd_scan returned None → retry next pass". It never considered deterministic failures.
- **Acceptance:** slice 3 "a temporary rspamd outage must not permanently skip mail". The code now gets the opposite failure: it permanently skips *all later* mail.
- **Tests:** `test_bookmark_stops_at_rspamd_failure` and `test_missing_body_does_not_advance_bookmark` pin the halt. No test checks that the halt ends.

**Recommendation.** Count consecutive failures on the *same* UID in `AccountState`. After N attempts over at least a minimum time, check rspamd with a tiny synthetic probe message on the same `/checkv2` path:

- If the probe scores, rspamd is healthy and the UID is poison. Mark it terminal (`scan_giveup` event, `our_action='scan_giveup'`, leave it in place — fail closed), advance past it, and keep catch-up from retrying it.
- If the probe fails, it's an outage. Keep halting exactly as today.
- An empty body that repeats N times over the same window is terminal without a probe.

**Tests to add:** poison UID in the middle → bookmark advances past it after N passes and later UIDs are scored; full outage (probe fails) → never gives up; repeated empty body → gives up; Junk poison does not block a later user-move learn; catch-up skips `scan_giveup` rows.

### OPUS-CR-003 — Provider-Junk rescue ignores Microsoft's spoof verdict

**Severity:** High (only when `mode: move`) · **Status:** Verified (code path)
**Files:** `filter/filter.py` `poll_junk` (3991-4018), `_queue_junk_rescue` (3726-3748), `execute_due_rescues` (3751-3840)

**What happens.** In move mode, provider-Junk is rescued to Inbox when it scores under the threshold **or matches an allow entry**. Allow matching reads the From and Sender headers. Those are exactly what a spoofer controls; Reply-To was dropped as "spoofable", but From is no harder to fake. The rescue path never checks the trusted Microsoft `Authentication-Results`, which the code already parses for bucket B. So:

- A phishing mail forging an allowlisted vendor address (`ap@vendor.com`) or an internal roster domain (`@rjmetalfab.com`, a CEO-fraud pattern) that Microsoft junked for `compauth=fail` / `dmarc=fail` gets **pulled back into Inbox**. With the live `move_grace_seconds: 0` that happens immediately.
- The same holds for a low-scoring spoof without an allow hit.

This is the business-email-compromise path the allowlist was meant to protect against. Here the allowlist overrides Microsoft's positive spoof detection.

**Trace**

- **Requirement:** `new_requirements.md` §1 (lists as a safety rail for R&J business mail). IMPLEMENTATION_STATUS 2026-09-23: "Do trust Microsoft's edge SPF/DKIM/DMARC verdict when stamped as `Authentication-Results` with authserv-id `mx.microsoft.com`."
- **Architecture:** plan 23 dropped Reply-To for spoofability. The 2026-09-21 rescue added "allowlisted may be rescued" without an auth condition.
- **Implementation:** `_m365_method_passes()` exists, but only bucket-B score suppression uses it.
- **Tests:** `test_provider_junk_allowlist_rescues_even_if_high_score` pins "rescue regardless". No spoof test.

**Recommendation (fix):** never rescue a message whose outermost *trusted* Microsoft AR says `compauth=fail`, or `dmarc=fail` when there is no compauth token. Check this when the rescue is queued (so shadow's `would_rescue` log is accurate) and again at execution time. This only ever *prevents* a MOVE, so it fails closed.

**Recommendation (policy, for the operator):** decide whether an allow hit should keep a Microsoft-flagged spoof in Inbox (`scan_inbox` path). I did **not** change that: it can junk mail the user explicitly trusts, such as Outlook Safe Senders overriding spoof intelligence.

**Tests to add:** allowlisted plus `compauth=fail` stays in Junk (move mode) and logs `rescue_skipped_spoof`; the same message in shadow logs no `would_rescue`; an untrusted AR claiming failure does not change behavior; `compauth=pass` still rescues.

### OPUS-CR-004 — Rspamd neural autotrains on unadjusted scores and on every re-scan

**Severity:** High (classifier integrity) · **Status:** Verified (rspamd 4.2.0 source)
**Files:** `rspamd/local.d/neural.conf`; `filter/filter.py` `apply_m365_auth_trust` (416-450)

**What happens**

- `neural.conf` enables neural with `train { spam_score = 8; ham_score = -2 }`, and rspamd 4.2.0 defaults `autotrain = true`. The neural postfilter pushes a training vector on **every** `/checkv2` task, using **Rspamd's own score**.
- Bucket B (`apply_m365_auth_trust`) removes DKIM/SPF/DMARC failure weight **in Python, after** Rspamd has scored. So Rspamd's internal score for legitimate M365-delivered mail (the Amazon/Google cases, about 18-25 internally) stays ≥ 8. **Neural keeps learning those as spam** and later adds `NEURAL_SPAM`, which bucket B does not suppress.
- Every extra `/checkv2` call is a training event. That includes provider-Junk scoring (since 2026-09-21), list-hit scoring, catch-up, `explain_score.py`, and the one-off **re-score of about 2,897 Trained-\* rows on 2026-09-24**. Trained-Ham mail with high internal scores became neural *spam* vectors.
- SESSION_HANDOFF records that the 2026-09-24 Bayes wipe deliberately **kept neural `rn_*` keys**. The pre-remediation poisoning is therefore still live.
- The README hard rule "No autolearn" covers only Bayes. That is technically true, but it misleads operators.

**Trace**

- **Requirement:** README "No autolearn ... Bayes only learns from explicit user moves". IMPLEMENTATION_STATUS buckets A/B/C: "Ham training cannot cancel A/B auth-header symbols"; the goal is sane scores before leaving shadow.
- **Architecture:** bucket B is a filter-side adjustment. Plan 26 records "Proofpoint-era neural/Bayes pollution".
- **Implementation:** there is no `autotrain`/`frozen` setting, and Rspamd has no way to see the filter's adjusted score.
- **Tests:** none possible in-repo. This is Rspamd behavior.

**Recommendation (operator decision; I did not change scoring config or Redis):**

1. Set `train { autotrain = false; }` (or `frozen = true;`, available in 4.2.0) in `neural.conf`, **or** disable neural until scores are validated.
2. Delete the `rn_*` keys (neural only; Bayes and fuzzy untouched), the same way the 2026-09-24 retrain was done.
3. Bump `unraid/bootstrap.version` so bootstrap refreshes `local.d`.
4. Optionally, move bucket-B suppression into Rspamd itself (a Lua rule that reads the trusted AR), so Rspamd's own score, action, and neural all agree with the filter.

---

## Medium

### OPUS-CR-005 — Dashboard list saves over 16 KiB fail with 413 in production

**Severity:** Medium · **Status:** Verified (real waitress server)
**Files:** `filter/dashboard.py:2078-2084` (`serve(..., max_request_body_size=LOGIN_REQUEST_MAX)`), `filter/dashboard.py:524-527`, `filter/test_dashboard.py:365-386`

Waitress enforces `max_request_body_size` **before** WSGI (`waitress/parser.py:154-160`). The per-route Flask override `request.max_content_length = LIST_POST_MAX` (256 KiB) never gets a chance to run. A 700-address save (form-encoded 22,437 bytes, under the 1,000-entry cap) returned **413 Request Entity Too Large** from waitress. The Flask test client skips waitress, so the list tests pass.

**Trace.** Slice 12 §4 says to raise the limit to 256 KiB "only on list POST views ... without loosening /login". Slice 8 wanted a server-level cap. The test `test_waitress_rejects_large_body_before_wsgi` pins the 16 KiB server cap, so the two slices contradict each other at the server layer.

**Recommendation.** Set the waitress ceiling to `LIST_POST_MAX` (still bounded, so nothing is unbounded before WSGI), and keep Flask's app-wide `MAX_CONTENT_LENGTH = 16 KiB`, raised only for `/lists/*` POST. `/login` stays at 16 KiB (covered by `test_login_request_body_is_limited`).

**Tests:** update the waitress test to pin `LIST_POST_MAX`; add an end-to-end test with real waitress: a list POST of about 22 KiB is *not* 413 at the server, and `/login` over 16 KiB is 413.

### OPUS-CR-006 — The scan-time Bayes key can come from the message's own `Delivered-To`

**Severity:** Medium (latent on ByteLord) · **Status:** Verified (rspamd 4.2.0 source)
**Files:** `filter/filter.py:2338-2344` (`rspamd_scan_detail`), `filter/filter.py:2423` (`rspamd_learn`)

Learning always prepends `Delivered-To: <identity>`. Scanning prepends it **only** when `bayes_user` has no `@`. With an address identity, or the default per-mailbox identity (`acc.user`), scanning relies on HTTP `Rcpt`. But in rspamd 4.2.0 the **first MIME `Delivered-To:`** in the message sets `task->deliver_to` (`mime_headers.c`), and `rspamd_task_get_principal_recipient()` gives `deliver_to` priority over `Rcpt` (`task.c:1392-1405`). Any delivered message that already carries a `Delivered-To:` gets classified against *that* address's notebook. Postfix, Dovecot LMTP, and Gmail add one to essentially all mail. Bayes then never applies at scan time, while learning keeps filling the right notebook. ByteLord uses the bare `bytelord`, so it is **not** affected today. Upstream's default configuration is.

**Trace:** Slice 9 says scan and learn must use the same identity; slice 6 says `Rcpt` = Bayes identity. The tests only check the `Rcpt` header.

**Recommendation.** Always prepend `Delivered-To: <bayes_user or acc.user>` on scan, the same as learn. Keep today's `Rcpt` choice so no other scoring changes.

**Tests:** an address `bayes_user` scan posts a body starting with `Delivered-To: <identity>`; a raw message that already has `Delivered-To: other@x` still gets the prepended identity first.

### OPUS-CR-007 — Re-junking a filter-rescued message is never learned

**Severity:** Medium (move mode) · **Status:** Verified (reproduced)
**Files:** `filter/filter.py` `_FILTER_OWNED_JUNK_ACTIONS` (3235-3243), `poll_junk` (3914-3916)

`rescued_to_inbox` counts as "filter-owned Junk". When the user decides a rescued message **is** spam and drags it back to Junk, the new Junk UID's SHA sibling (the Junk row marked `rescued_to_inbox`) makes `poll_junk` skip it as filter-owned. That is terminal, so there is no spam learn. This is the highest-value feedback (it corrects the filter's own mistake), and it is dropped. My reproduction printed `pending_learn = None learned = []`.

**Trace:** README "Inbox -> Junk = learn as spam"; the 2026-09-21 policy says "never train on rescue" (the *rescue* itself, not the user's later correction). Tests: none for rescue followed by a user re-junk.

**Recommendation.** Remove `rescued_to_inbox` from the filter-owned set. The rescued row already has `current_folder=INBOX`, so it counts as an Inbox sibling and the normal user-move path learns spam. Keep `pending_rescue` (a rescue still in flight) and the Inbox→Junk actions.

**Test:** rescued, then user re-junks → `pending_learn='spam'`; a filter move of rescued mail (Inbox row `moved_to_junk`) is still not learned.

### OPUS-CR-008 — Rescue reverses the user's own moves of old or non-Inbox mail into Junk

**Severity:** Medium (move mode) · **Status:** Verified (reproduced)
**Files:** `filter/filter.py` `poll_junk` (3971-4018)

Any new Junk UID with no Inbox sibling (no row, no fingerprint) is treated as provider-delivered. That also covers:

- mail the user moves into Junk from **Archive or other folders**;
- pre-install Inbox mail (older than the bookmark, so no row).

If Rspamd scores it under the threshold, move mode MOVEs it **back to Inbox** within `move_grace_seconds` (0 live) and learns nothing. My reproduction: a 90-day-old message moved into Junk was immediately moved to Inbox.

**Trace:** the 2026-09-21 policy "rescue under-threshold provider-Junk" assumes every non-sibling Junk arrival is provider-delivered. User intent is supposed to win ("fail closed"). Tests: none for provenance.

**Recommendation.** IMAP MOVE keeps INTERNALDATE, and provider-delivered Junk is fresh when `poll_junk` sees it. Only queue a rescue when `now - INTERNALDATE ≤ RESCUE_MAX_AGE_S` (3 days, enough to cover filter downtime). Otherwise log `rescue_skipped_old`. If INTERNALDATE is unknown, keep today's behavior.

**Tests:** a 90-day-old arrival is not rescued and logs `rescue_skipped_old`; a fresh arrival is still rescued; a missing INTERNALDATE is still rescued.

### OPUS-CR-009 — Junk retention can trash mail it should keep

**Severity:** Medium (flag/move) · **Status:** Verified (code path)
**Files:** `filter/filter.py` `_sweep_folder_to_trash` (4531-4592)

Retention runs `SEARCH BEFORE <cutoff>` on Junk and skips only rows already learned as ham. It can move to Trash:

1. **Junk UIDs `poll_junk` has not processed yet** (above the Junk bookmark). If a user drags an old message to Junk just before the hourly sweep and `poll_junk` isn't due, it is trashed before its spam learn is even scheduled.
2. **Rows with `pending_learn`** (a user move waiting out `learn_grace_seconds`). `process_pending_learns` then logs `pending_lost`, and the training is lost.
3. **Allowlisted provider-Junk in `flag` mode.** Rescue only runs in move mode; flag mode logs `would_rescue`. After `junk_retention_days` (default 10), allowlisted R&J mail sitting in Junk goes to Trash. The operator's next planned step is shadow → **flag**.

**Trace:** `new_requirements.md` priority driver ("must not be lost to Junk"); slice 1 retention starts at flag; README "Junk -> Inbox ... learn". Tests: retention internals are almost uncovered (only the mode gate).

**Recommendation**

- Only trash Junk UIDs at or below the Junk bookmark; skip the sweep when there is no bookmark.
- Skip rows with `pending_learn` in any swept folder.
- Skip Junk rows with `our_action` in (`allowlisted`, `pending_rescue`).
- Record `our_action='allowlisted'` on allow-hit Junk rows.

**Tests:** each exclusion, in flag mode.

### OPUS-CR-010 — Mail without a Message-ID bypasses the filter

**Severity:** Medium (depends on deployment) · **Status:** Verified (code path)
**Files:** `filter/filter.py` `_scan_inbox_uid_batch` (3414-3418), `poll_junk` (3899-3901)

A missing Message-ID is "terminal skip": no row, no score, no list match, no flag or move, and no learn from a user Inbox↔Junk move. This came from the pre-slice-5 schema, where Message-ID was the primary key. Slice 5 moved identity to IMAP coordinates, but the skip stayed (slice 3 §4 still lists it). Spam that simply leaves out Message-ID passes straight through on servers that don't add one (Postfix/Dovecot don't for remote mail). Exchange Online usually stamps one, so the live impact is small.

**Recommendation.** Keep the `no_message_id` audit event, but process the message normally with `message_id=NULL`. The pending-move row stores `""`, because that column is `NOT NULL`.

**Tests:** a no-Message-ID Inbox message is scored and flagged/queued; a no-Message-ID user Inbox→Junk move is learned; catch-up handles such rows.

### OPUS-CR-011 — Deferred transactions can fail with `SQLITE_BUSY` under concurrency

**Severity:** Medium · **Status:** Verified (SQLite WAL semantics); Plausible trigger rate
**Files:** `filter/filter.py` `Db.tx()` (1605-1613)

`Db.tx()` issues `BEGIN` (deferred). Many transactions *read first*:

- `log_event` looks up the subject before INSERT;
- `_set_learn_retry` reads before UPDATE.

In WAL mode, upgrading a read snapshot to a write fails **immediately** with `SQLITE_BUSY` (`BUSY_SNAPSHOT`) if another connection committed in between. The 30 s busy timeout does **not** apply. There are 10 account threads plus the dashboard writing to one DB. The resulting `sqlite3.OperationalError` falls into `_run_account`'s generic handler, which forces an IMAP reconnect. If a MOVE had already succeeded, the matching DB update is lost for that pass.

**Recommendation.** `BEGIN IMMEDIATE`. It takes the write lock up front and honors the busy timeout.

**Tests:** `tx()` issues `BEGIN IMMEDIATE`; a two-connection test where the second writer waits instead of raising.

### OPUS-CR-012 — Reconnect backoff resets on every successful connect

**Severity:** Medium · **Status:** Verified (code path)
**Files:** `filter/filter.py` `_run_account` (4614-4737)

`backoff = RECONNECT_MIN_BACKOFF` runs right after connect, before any work. A failure that repeats inside the loop means: connect, reset to 5 s, the iteration raises, sleep 5 s, and around again. Examples: `KeyError` on a missing `UIDVALIDITY`, a server error on `STORE`, a bug on one message. The result is an **M365 login through the OAuth proxy every ~5 s, forever**. That invites Exchange throttling of the whole tenant app.

**Recommendation.** Reset the backoff only after a full loop iteration completes, just before `wait_between_scans`.

**Test:** a fake client whose `scan_inbox` always raises gets growing backoff (5, 10, 20…).

### OPUS-CR-013 — The learn budget is checked after downloading full bodies

**Severity:** Medium · **Status:** Verified (code path)
**Files:** `filter/filter.py` `_drain_train_folder` (4129-4218), `process_pending_learns` (4028-4126)

`try_learn` applies `max_learns_per_hour` only after the body is fetched.

- A Train-Spam drop of 500 messages (live `max_train_per_run` 5000, default `max_learns_per_hour` 50) downloads **every body** per pass. It learns 50, and the rest get per-object backoff (30 s, 60 s, … 1 h) and are downloaded again on each backoff tick.
- `process_pending_learns` does the same for a mass Inbox→Junk move.

That is heavy, avoidable IMAP traffic through the proxy, which Exchange throttles.

**Recommendation.** Compute the remaining hourly learn budget first:

- Train drains fetch at most `min(max_train_per_run, budget)` UIDs.
- Pending learns stop fetching when the budget hits 0, and record the retry state DB-only (so `prune_stale_pending_learn` still keeps them).

**Tests:** with the budget used up, a drain issues no `BODY.PEEK[]`; pending learns past the budget get retry state without a fetch.

### OPUS-CR-014 — MOVE-as-COPY leftovers are handled only for list folders

**Severity:** Medium · **Status:** Plausible (needs live M365 verification)
**Files:** `filter/filter.py` `_drain_train_folder` (4200-4217), `execute_due_moves` (3701-3723), `execute_due_rescues` (3820-3840)

The 2026-09-21 code says "Microsoft 365 / Exchange IMAP often implements MOVE as COPY and leaves the original". Only list drains react (`_move_clearing_source`). If that server behavior is real:

- **Train-\*:** the leftover UID is found again next pass; `try_learn` short-circuits (`learned_as == kind`), and it is **MOVEd again** — a duplicate in Trained-\* on every loop (~30 s).
- **Inbox→Junk / rescue:** the source copy stays visible in Inbox/Junk and is never re-processed (it is below the bookmark). In move mode, spam would stay in Inbox.

**Recommendation.**

- **Train-\* (fix, non-destructive):** skip UIDs whose DB row already records `current_folder == Trained-*`, instead of re-learning and re-moving them, and log it.
- **Inbox→Junk and rescues (verify live before promotion):** after `client.move`, check for leftovers the way list drains do. Log `move_left_source` rather than expunging Inbox/Junk. Deciding whether to expunge there is a policy question for the operator.

**Tests:** a Train-\* leftover is not moved twice.

### OPUS-CR-015 — Core learning and move-mode executors have no tests

**Severity:** Medium (test adequacy) · **Status:** Verified (coverage)

Branch coverage shows these requirement-critical paths are **never executed** by the suite:

- `scan_inbox` Junk→Inbox revert → ham (`filter.py:3441-3471`), the README's second training rule;
- `poll_junk` user Inbox→Junk without `$Junk` → `pending_spam` (`3956-3964`), the primary spam feedback;
- `execute_due_rescues` except the happy path (`3775-3826`), and `execute_due_moves` missing, oversize, rate, and failure branches (`3660-3713`);
- `process_pending_learns` folder-mismatch, lost, and oversize branches (`4083-4118`);
- safe-mode enter and exit (`3333-3347`); `check_rate` refusal (`2972-2983`); `try_learn` safe-mode and rate branches (`3072-3087`);
- retention search, exclusion, and failure branches (`4536-4591`); `vacuum_if_due` and `prune_stale_pending_learn` (`2244-2287`);
- `_run_account` loop order and backoff (`4611-4737`); `scan_inbox` bookmark initialization (`3293-3306`).

**Recommended new tests.** Beyond those added with each fix below:

1. User Inbox→Junk (no keyword) → grace → `process_pending_learns` → `learn_spam`.
2. Junk→Inbox revert → `pending_ham` → learn; `$NotJunk` skips grace.
3. Pending spam where the user moved it out of Junk during grace → `pending_lost`.
4. `execute_due_moves` where the rate quota allows only part of the batch.
5. `scan_inbox` UNSEEN over the cap → safe-mode "all", no bookmark advance; then under the cap → exits.
6. `_run_account` with a scripted fake: call order is drains → scan → moves → rescues → junk poll → retention.
7. `retention_sweep` in flag mode respects `exclude_learned_ham`.

---

## Low

### OPUS-CR-016 — `pending_move_canceled` is logged for every allowlisted message
`filter/filter.py:3500-3513`. The allow branch always logs `pending_move_canceled`, even when there was no pending move. On a live system with many allow hits, the Events page fills with fake cancellations, which hides the real CR-001-style cancellations the operator needs to see. **Fix:** have `drop_pending_move` return the row count, and log only when it is > 0.

### OPUS-CR-017 — `bootstrap.sh` renders secret configs under the default umask
`unraid/bootstrap.sh:149-166`. `render_subst` writes `${dest}.tmp` with `>` under the caller's umask (usually 022, so 0644). It then `mv`s the file, and `verify_secret_file` chmods to 0640 afterwards. That leaves a short window where the Redis and controller passwords are world-readable on the host. Line 39's fallback `|| echo 9` is also stale (the version is 10). **Fix:** run the awk render in a `umask 077` subshell, and make the fallback track `bootstrap.version`.

### OPUS-CR-018 — The dashboard "routing catch rate" formula is now wrong
`filter/dashboard.py:1503`. `routed = scans + allowlisted + blocklisted`. Since 2026-09-21, list hits are *also* scanned (a `scan` event), so list hits count twice. Provider-Junk scans (also `scan` events) also go into the denominator, while the numerator only counts Inbox→Junk moves. **Recommendation:** include the folder in the `scan` event detail and compute Inbox-only moves / Inbox-only scans. Or relabel the metric.

### OPUS-CR-019 — HTTP `Rcpt` is the first To/Cc address, not the mailbox
`filter/filter.py:3182` (`first_recipient(raw, acc.user)`). README ("`Rcpt` stays the mailbox address"), IMPLEMENTATION_STATUS ("mailbox is HTTP `Rcpt`"), and commit `05c9c93` ("Send the mailbox as Rcpt") all say the IMAP mailbox is sent. The code sends the first To/Cc address, which is attacker-controlled and is not the mailbox for BCC, list, or alias mail. This affects Rspamd recipient rules (for example `FORGED_RECIPIENTS`). **Operator decision:** switching to `acc.user` is more truthful, but may add points to list and BCC ham. Update either the docs or the code, deliberately.

### OPUS-CR-020 — Allow/Block drags learn twice
An Allowlist drag learns ham, then MOVEs to Inbox. If the message came from Junk, `scan_inbox` sees a Junk sibling and treats it as a user revert, so it learns ham *again*. A Blocklist drag of Inbox mail learns spam; `poll_junk` then finds the Inbox sibling and learns spam again. Rspamd answers 208 (no double training), but each extra learn uses the hourly budget and adds a second `learn_*` event, which inflates the dashboard learn counters. **Recommendation:** in the revert and user-move paths, skip siblings whose drain row shows `our_action` in (`allowlisted`, `blocklisted`) and `learned_as` already equal to the kind.

### OPUS-CR-021 — The list parser accepts patterns that can never match
`filter/filter.py:709-735`. It accepts `<a@b.com>`, `a@b.com>`, `@.`, `x..y`, and hosts without a dot. Rows are stored silently and never match, and the admin assumes the entry works. **Recommendation:** reject `<>()[];:,"'` and require a host label with a dot (allowing `localhost`-style hosts only if wanted). Test the error line numbers.

### OPUS-CR-022 — ByteLord Compose settings
`deploy/bytelord-compose.yaml`:

- `TZ: Europe/Berlin` is an upstream leftover. Dashboard timestamps and the per-day scan buckets show Berlin time; the operator's commits are UTC-7.
- `env_file` injects `REDIS_PASSWORD` (which the filter never uses) into the container environment, where it shows in `docker inspect`. The secrets file is already mounted.
- There is no `stop_grace_period`. Docker SIGKILLs after 10 s, while account threads may need up to one 30 s IDLE chunk plus logout, so a MOVE can be cut off mid-update.

**Recommendation:** set `TZ` to the operator's zone, drop `env_file`, and add `stop_grace_period: 60s`.

### OPUS-CR-023 — Dashboard login throttling sees a single client IP behind Caddy
`filter/dashboard.py:136-138`. The trusted-proxy default is loopback only, but host Caddy reaches the container as the Docker bridge gateway (IMPLEMENTATION_STATUS notes this for the controller). `X-Forwarded-For` is ignored, so every NetBird client shares one throttle key: 25 failures in 60 s lock *everyone* out for a minute. **Recommendation:** set `DASHBOARD_TRUSTED_PROXIES=<bridge gateway>/32` in the ByteLord compose.

### OPUS-CR-024 — `redact_log` deletes useful diagnostics
`filter/filter.py:140`. `(?i)\bLOGIN\b[\s\S]*` replaces everything from any "login" onward. For example, `"[AUTHENTICATIONFAILED] Login failed: account locked"` becomes `"[AUTHENTICATIONFAILED] LOGIN ***"`. **Recommendation:** only redact the arguments after an IMAP `LOGIN` command token (`LOGIN <user> <password>`). Keep the explicit-secret replacement.

### OPUS-CR-025 — `DASHBOARD_USERS` comma splitting
`filter/dashboard.py:246`. Env entries are split on `,`, while `_parse_user_line` also accepts `,` between account names. `alice:hash:acct1,acct2` becomes a user scoped to `acct1` plus a garbage entry `acct2`. The helper writes pipes, so this only affects hand-written env values. **Recommendation:** document "pipes only in env", or split entries on `;`.

### OPUS-CR-026 — List editor Cancel after a validation error
`filter/lists.js`. After a 400, the page's snapshot is the *rejected* text, so Cancel does not bring back the saved list. **Recommendation:** render the saved list in a `data-saved` attribute, and have Cancel reload `?scope=&kind=`.

### OPUS-CR-027 — Maintainability bundle

- Dashboard `list_dashboard_save` events use account `_dashboard`, which no worker ever prunes.
- `_migrate`'s legacy rebuild uses `executescript`, which commits between steps, so a crash mid-migration can orphan `messages_imap` (legacy upgrade path only).
- `execute_due_rescues` runs twice per loop (inside `poll_junk` and again in `_run_account`).
- `dashboard._list_page` rewrites the global `filter.DB_PATH` on every request.

### OPUS-CR-028 — Documentation drift

- README "Folder discovery" says the filter cares about **seven** folders; it now manages nine (Allowlist/Blocklist).
- The status tables in `design-arch/allow_block_sliced_plan.md` and slices 9–12 still say "ready to implement".
- Slice 10 §7, slice 12, and the parent plan say list hits skip `/checkv2`. That was superseded on 2026-09-21. IMPLEMENTATION_STATUS says to trust the code, but the slice docs are what a new agent reads first.

### OPUS-CR-029 — The rspamd.com fuzzy rule never loaded (Medium; found during deploy)

Found on 2026-09-25 while the operator ran the deploy runbook. I missed it in the original pass because I read the rspamd `local.d` files for policy and did not validate their values. `rspamadm configdump` printed `bad encryption key value: ftcvm5dg…`. `rspamd/local.d/fuzzy_check.conf` (added 2026-08-26) overrode the stock `rspamd.com` rule with an `encryption_key` that is not a real rspamd key. It contains `v`, which is not in rspamd's zbase32 alphabet, and the stock key is `icy63itbhhni8bq15ntp5n5symuixf73s1kpjh6skaq4e7nx5fiy`. In rspamd 4.2.0, `fuzzy_parse_rule()` returns -1 on a bad key before the rule is added to `fuzzy_rules` (`src/plugins/fuzzy_check.c`, "bad encryption key value" then `g_ptr_array_add` only at the end), so the only fuzzy rule was silently dropped. **No `FUZZY_*` symbol has ever fired.** The same file also replaced the stock SRV discovery (`service=fuzzy+rspamd.com`) with a hand-written host, and used the legacy `max` key, which in 4.x means "hits to saturate", not points.

---

## Controls checked and found consistent

- Shadow never automatically MOVEs, flags, or expires Inbox/Junk/Trash. Train-\* and list-folder writes are the documented exceptions, and Inbox/Junk polling SELECTs are EXAMINE in shadow.
- Fetch discipline: every automatic body fetch goes through `fetch_under_cap`, including `explain_score.py`.
- The Inbox bookmark advances only across a terminal prefix. UIDVALIDITY changes clear the bookmark, pending moves, and pending learns.
- The CR-001 (ChatGPT) allow re-check before a due move is present and tested.
- List precedence (user address → user `@host` → domain address → domain `@host`; allow wins only on a true tie) matches plan 23. The contradictory-learn skip matches plan 27.
- Config validation is strict (types, ranges, unknown keys, and `tls_mode: none` only for loopback or with explicit opt-in).
- Dashboard: CSP `script-src 'self'`, CSRF on list POSTs, admin-only list routes, output escaping, `next=` validation, session idle/absolute expiry, and `Vary: Cookie`.
- Secrets: the protected secrets-file contract; log redaction on IMAP exception paths; SQLite directory 0700, files 0600.
- Bucket-B trust logic uses only the outermost Authentication-Results header and requires a clean pass per method (well tested).

## Out of scope / not reported

- OAuth2 in the filter, dashboard authentication strength, and public exposure (the service is private to NetBird).
- Unraid templates and `deploy/fix-cursor-apparmor.sh` (the operator's CR-002 annotation).
- Supply-chain pinning (ChatGPT IMAP-CR-016, accepted risk).
