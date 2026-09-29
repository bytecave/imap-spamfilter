# Claude Fable 5.1 code and security review — imap-spamfilter

**Reviewed:** 2026-09-28/29 (Pacific), `main` at `f53586a`
**Reviewer:** Claude Fable 5.1 (Claude Code), Agent Mail name `GoldenSpring`
**Scope:** `filter/*.py` (filter, dashboard, bootstrap and explain CLIs, and every test file), `filter/lists.js`, `filter/Dockerfile`, the compose files, `deploy/`, `rspamd/local.d/`, `redis/`, `.github/workflows/`, `accounts.yml.example`
**Fix log:** [`CLAUDE_FABLE5.1_CODE_FIXED.md`](CLAUDE_FABLE5.1_CODE_FIXED.md)

## Contents

- [How this review was done](#how-this-review-was-done)
- [Severity guide](#severity-guide)
- [Findings index](#findings-index)
- [High](#high)
- [Medium](#medium)
- [Low](#low)
- [Info and operator recommendations](#info-and-operator-recommendations)
- [Prior findings that are still open](#prior-findings-that-are-still-open)
- [Traceability: requirement → architecture → acceptance → code → tests](#traceability-requirement--architecture--acceptance--code--tests)
- [Recommended new tests](#recommended-new-tests)
- [Controls checked and found correct](#controls-checked-and-found-correct)

## How this review was done

1. **Reading, in chronological order.** I read `README.md`, `code_review_orientation.md`, `IMPLEMENTATION_STATUS.md` and `SESSION_HANDOFF.md`, then the Cursor plans in `cursor_plans/` oldest to newest (2026-08-14 → 2026-09-28), then the design slices in `design-arch/`. Later documents overrule earlier ones, for example:
   - List hits score again since 2026-09-21. The 2026-09-06 "skip `/checkv2` on a list hit" plan is superseded.
   - Reply-To was dropped from list matching on 2026-09-05.
   - The shadow-review web page plan was never built; the Flask dashboard took its place.
   - The Sif UI and AppArmor plans belong to other projects.
2. **Code.** I read `filter/filter.py` (5,630 lines) in full myself. Two read-only subagents reviewed `dashboard.py`/`lists.js` and the ops tooling/configuration. The second one also read the rspamd **4.2.0** tag sources (`bayes_expiry.lua`, `symcache_runtime.cxx`, `protocol.c`, `task.c`). I re-checked every claim of theirs that I report here against the code or those sources, and dropped anything I could not confirm.
3. **Reproduction.** Every High finding was reproduced in the production runtime: the `python:3.12-slim` container with the pinned `requirements.txt` (imapclient 3.1.0, requests 2.34.1), against a local HTTP stub and a fake IMAP server that honours IMAP literals. Scripts and outputs are summarised under each finding.
4. **Live system, read-only.** Everything I touched on the live system was read-only:
   - `SELECT`s of event counts and score-detail symbol names against the live SQLite database (`?mode=ro`, no message bodies, no subjects printed);
   - two non-secret environment variables in the filter container;
   - `docker logs spamfilter-rspamd`;
   - the Unbound forwarder file inside its container.

   I did not restart, rebuild, move, learn or expunge anything.
5. **Prior reviews.** `CHATGPT_CODE_REVIEW.md` and the two `CLAUDE_OPUS5.5_EXTRA_*` files are deleted from the working tree but still in `HEAD`. I read them from git so I would not re-file closed items. Open prior items are listed [separately](#prior-findings-that-are-still-open).
6. **Baseline tests.** `429 passed` with the whole repository mounted (see [FABLE-CR-026](#fable-cr-026--the-orientation-test-command-fails-three-tests)).

Out of scope, per the operator: lack of OAuth2 / username-password strength on the dashboard (NetBird-only), neural (off by decision), supply-chain pinning (accepted risk), Unraid deployment, and the Outlook add-in (not built yet).

## Severity guide

| Severity | Meaning |
|---|---|
| **High** | Mail can bypass the filter, a mailbox can be stalled indefinitely, or list/Bayes state can be corrupted by ordinary input |
| **Medium** | Wrong outcome in a realistic but narrower situation, or a documented behaviour that does not happen |
| **Low** | Robustness, diagnostics, fail-open edge in an operator tool, or a test that does not prove its claim |
| **Info** | Observation or recommendation; no defect against the written policy |

Reachability is given per finding as **shadow**, **flag/move**, or **dashboard**.

## Findings index

| ID | Severity | Area | Title |
|---|---|---|---|
| [FABLE-CR-001](#fable-cr-001--expire--0-makes-rspamd-delete-bayes-tokens-every-minute) | High | rspamd config | `expire = 0` makes rspamd delete Bayes tokens every minute |
| [FABLE-CR-002](#fable-cr-002--a-message-id-can-inject-an-imap-literal-and-wedge-an-account) | High | `filter.py` | A Message-ID can inject an IMAP literal and wedge an account |
| [FABLE-CR-003](#fable-cr-003--an-8-bit-fromtocc-address-makes-every-rspamd-scan-fail) | High | `filter.py` | An 8-bit From/To/Cc address makes every rspamd scan fail |
| [FABLE-CR-004](#fable-cr-004--rspamd-stops-evaluating-rules-once-a-score-passes-reject) | Medium | `filter.py` ↔ rspamd | rspamd stops evaluating rules once a score passes `reject` |
| [FABLE-CR-005](#fable-cr-005--unbound-forwards-every-lookup-to-cloudflare-so-the-dns-blocklists-refuse-the-queries) | Medium | deploy (Unbound) | Unbound forwards every lookup to Cloudflare, so the DNS blocklists refuse the queries |
| [FABLE-CR-006](#fable-cr-006--list-editor-cancel-after-a-400-saves-the-text-into-a-different-list) | Medium | dashboard | List editor: Cancel after a 400 saves the text into a different list |
| [FABLE-CR-007](#fable-cr-007--a-stale-list-page-reverts-outlook-allowlistblocklist-drags) | Medium | dashboard | A stale list page reverts Outlook Allowlist/Blocklist drags |
| [FABLE-CR-008](#fable-cr-008--oversize-train-ham-never-gets-its-inbox-copy) | Medium | `filter.py` | Oversize Train-Ham never gets its Inbox copy |
| [FABLE-CR-009](#fable-cr-009--a-train-ham-copy-of-junk-mail-is-treated-as-a-user-revert) | Medium | `filter.py`, tests | A Train-Ham copy of Junk mail is treated as a user revert |
| [FABLE-CR-010](#fable-cr-010--the-locally-built-filter-image-carries-the-upstream-authors-registry-name) | Medium | deploy | The locally built filter image carries the upstream author's registry name |
| [FABLE-CR-011](#fable-cr-011--microsoft-authentication-results-trust-applies-to-every-account) | Medium (latent) | `filter.py` | Microsoft Authentication-Results trust applies to every account |
| [FABLE-CR-012](#fable-cr-012--an-8-bit-message-id-erases-subject-and-from-too) | Low | `filter.py` | An 8-bit Message-ID erases Subject and From too |
| [FABLE-CR-013](#fable-cr-013--a-yaml-syntax-error-prints-the-offending-accountsyml-line-password-included) | Low | `filter.py` | A YAML syntax error prints the offending `accounts.yml` line, password included |
| [FABLE-CR-014](#fable-cr-014--the-user-add-helper-grants-admin-to-a-blank-scope) | Low | dashboard CLI | The user-add helper grants admin to a blank scope |
| [FABLE-CR-015](#fable-cr-015--a-non-ascii-csrf-token-returns-500) | Low | dashboard | A non-ASCII CSRF token returns 500 |
| [FABLE-CR-016](#fable-cr-016--the-users-file-is-rewritten-in-place) | Low | dashboard CLI | The users file is rewritten in place |
| [FABLE-CR-017](#fable-cr-017--a-bad-users-file-byte-returns-500-instead-of-failing-closed) | Low | dashboard | A bad users-file byte returns 500 instead of failing closed |
| [FABLE-CR-018](#fable-cr-018--the-error-caret-is-off-by-one-after-a-leading-blank-line) | Low | dashboard | The error caret is off by one after a leading blank line |
| [FABLE-CR-019](#fable-cr-019--the-rspamd-stats-fetch-would-forward-the-controller-password-on-a-redirect) | Low | dashboard | The rspamd stats fetch would forward the controller password on a redirect |
| [FABLE-CR-020](#fable-cr-020--bootstrap_trainpy-exit-codes-and-readme-folder-paths) | Low | `bootstrap_train.py` | `bootstrap_train.py` exit codes and README folder paths |
| [FABLE-CR-021](#fable-cr-021--the-readme-points-bootstrap_trainpy-at-the-live-train-ham-folder) | Low | docs / CLI | The README points `bootstrap_train.py` at the live Train-Ham folder |
| [FABLE-CR-022](#fable-cr-022--explain_scorepy---message-id-can-explain-the-wrong-message) | Low | `explain_score.py` | `explain_score.py --message-id` can explain the wrong message |
| [FABLE-CR-023](#fable-cr-023--list-folder-drains-log-a-list_imap_add-event-every-pass) | Low | `filter.py` | List-folder drains log a `list_imap_add` event every pass |
| [FABLE-CR-024](#fable-cr-024--generic-compose-and-gitignore-gaps) | Low | deploy / repo | Generic compose and `.gitignore` gaps |
| [FABLE-CR-025](#fable-cr-025--dashboard-tests-that-pass-with-the-check-removed) | Low | tests | Dashboard tests that pass with the check removed |
| [FABLE-CR-026](#fable-cr-026--the-orientation-test-command-fails-three-tests) | Low | docs / tests | The orientation test command fails three tests |
| [FABLE-CR-027](#fable-cr-027--stale-hard-rules-and-read-only-wording) | Low | docs | Stale "hard rules" and "read-only" wording |
| [FABLE-CR-028](#fable-cr-028--oversize-mail-bypasses-allowblock-routing) | Info | `filter.py` | Oversize mail bypasses allow/block routing |
| [FABLE-CR-029](#fable-cr-029--expected-token-expiry-reconnects-dominate-conn_error) | Info | ops | Expected token-expiry reconnects dominate `conn_error` |
| [FABLE-CR-030](#fable-cr-030--dashboard-session-cookie-is-not-secure-in-production) | Info | deploy | Dashboard session cookie is not `Secure` in production |
| [FABLE-CR-031](#fable-cr-031--other-dashboard-observations) | Info | dashboard | Other dashboard observations |
| [FABLE-CR-032](#fable-cr-032--rspamd-ci-and-image-hardening-recommendations) | Info | rspamd / CI / image | rspamd, CI and image hardening recommendations |

---

## High

### FABLE-CR-001 — `expire = 0` makes rspamd delete Bayes tokens every minute

**Severity:** High · **Reachable:** every account (the shared `bytelord` notebook), continuously · **Status:** verified in rspamd 4.2.0 source and in the live rspamd log
**Files:** `rspamd/local.d/classifier-bayes.conf:7` (`expire = 0;`); README "Persistent data" ("Bayes `expire = 0`. That is intentional"); `design-arch/slice7_ops_secrets_supply_chain.md` §6

**What happens.** In rspamd 4.2.0 `src/plugins/lua/bayes_expiry.lua`:
- `check_redis_classifier` switches the Bayes expiry module on whenever the classifier's `expire` is a number. The stock `statistic.conf` sets none, so it is normally off. `0` is a number.
- The primary controller then runs `expire_step` every minute over 1,000 `RS*_*` token keys.
- For "infrequent" tokens (a count below the step's mean, dominated by one class) and "insignificant" ones, `set_ttl()` runs `EXPIRE key 0`. Redis deletes a key immediately on a non-positive EXPIRE.

So the rare, class-specific tokens are deleted within one cycle of being learned. That is new campaign vocabulary, exactly what lets Bayes tell spam from ham. The learn counters (`RSbytelord`) are untouched, so `min_learns` still reads as satisfied and nothing looks wrong on the dashboard.

**Live evidence** (read-only `docker logs spamfilter-rspamd`).
- 452 completed expiry cycles since 2026-09-23 06:57 report **374,738 tokens with "ttls set"**, which means deleted.
- Of those, 361,172 were on 2026-09-25, the day of the retrain3 re-feed. Other days range from 259 to 5,159.
- The current cycle checks about 13,800 token keys. The handoff recorded 78,042 `RS*` keys at the 2026-09-25 deploy and an unexplained 60,919 that night.

**Why a re-feed does not repair it.** rspamd's learn cache answers HTTP 208 (`already`) for a message it has learned before, so `bootstrap_train.py --all-trained` counts the lost messages as `already` and restores no tokens. Recovery needs an operator decision: clear the `bytelord` notebook including its learn cache, then re-learn from Trained-*. Retention has already moved older `rich_bytecave` Trained-* mail to Deleted Items.

**Trace.**
- Requirement: tokens are kept. The README says `noeviction` because "LRU would silently drop tokens and degrade accuracy".
- Architecture: slice 7 §6 "Bayes `expire = 0`", meant as never expire.
- Implementation: the value turns rspamd's expiry *on*, with a zero TTL.
- Tests: none read the rspamd config.

**Fix.**
- Remove the `expire` line (stock behaviour: never expire).
- Correct the README and slice 7.
- Bump `unraid/bootstrap.version` so bootstrap refreshes `local.d`.
- Add a config test.

Deploying it needs the file copied into the live `local.d` and `spamfilter-rspamd` restarted; Redis is untouched. That is the operator's step.

### FABLE-CR-002 — A Message-ID can inject an IMAP literal and wedge an account

**Severity:** High · **Reachable:** shadow, flag and move (every account) · **Status:** reproduced
**Files:** `filter/filter.py` `_identical_copies_in_folder` (5030-5084, SEARCH at 5064); callers `_clear_train_leftovers` (4746), `_restore_train_ham_to_inbox` (4864), `_drain_list_folder` (5193)

**What happens.** `_identical_copies_in_folder` puts the sender-controlled Message-ID straight into `client.search(["HEADER", "Message-ID", needle])`. imapclient 3.1.0 only quotes a value that contains a space, `"` or `\`:

| Message-ID (from the header) | Bytes imapclient sends | Server behaviour |
|---|---|---|
| `<abc{5}>` → `abc{5}` | `SEARCH HEADER Message-ID abc{5}` + CRLF | A synchronizing literal: the server sends `+` and waits for 5 more bytes |
| `<abc` CRLF ` def@x>` (folded) → `abc\r\n def@x` | `"abc` CRLF ` def@x"` | The command is split into two lines |
| raw 8-bit bytes | (`UnicodeEncodeError` in imapclient) | Not an `IMAPClientError` |

With the literal marker the client blocks until the socket timeout (60 s in `connect_imap`). That raises `TimeoutError`, an `OSError`, which the `except IMAPClientError` blocks do not catch, so it escapes to `_run_account`, which logs `connection error` and reconnects.

**Why it wedges the account.** The drains run before `scan_inbox` on every pass (`_run_account` 5405-5418):
- The Train-* leftover row keeps its Message-ID.
- A Train-Ham row is not marked `inbox_copied` until the check succeeds.
- A list-folder message stays in the folder until the check succeeds.

So every pass hits the same message, times out and reconnects, and backoff grows to 300 s. **That account's Inbox is never scanned again, and move mode never moves spam,** until someone removes the message by hand.

**How a mailbox gets there.** Users drag spam into Train-Spam and Blocklist, which is exactly the attacker-controlled input. Exchange's MOVE-as-COPY leaves a Train-* leftover after most drains, and `_clear_train_leftovers` runs the SEARCH on it on the next pass. Train-Ham and Allowlist hit it on the first pass.

**Reproduction** (Python 3.12, imapclient 3.1.0, fake server that honours `{n}` literals):

```text
'abc{5}'  -> [b'HEADER', b'Message-ID', b'abc{5}']
'a\r\n b@x' -> [b'HEADER', b'Message-ID', b'"a\r\n b@x"']
identical_copies RAISED TimeoutError timed out after 5.1s | IMAPClientError? False | OSError? True
```

**Trace.**
- Requirement: fail closed, and run 24/7 (README hard rules).
- Architecture: slice 5, "Message-ID is metadata, not identity", plus OPUS-CR-001, "Message-ID is sender-controlled".
- Implementation: CR-001 made the Message-ID untrusted for *deciding* identity, but it still reaches the IMAP protocol unquoted.
- Tests: no test uses a hostile Message-ID.

**Fix.**
- Before searching, allow only printable ASCII with no whitespace, `"`, `\`, `{`, `}`, `(` or `)`.
- Anything else skips the server search and counts as "no verified copy", which is already the safe fallback everywhere:
  - a list drain MOVEs normally;
  - a Train leftover is kept and warned about;
  - Train-Ham makes its (one-time) copy.

### FABLE-CR-003 — An 8-bit From/To/Cc address makes every rspamd scan fail

**Severity:** High on generic IMAP; Medium on this M365 deployment (no `scan_failed` events in the last 7 days of live data) · **Reachable:** shadow, flag and move · **Status:** reproduced
**Files:** `filter/filter.py` `rspamd_scan_detail` (2402-2468; HTTP headers at 2428-2435), `first_recipient` (2924-2933); consequences in `_scan_inbox_uid_batch` (3802-3813), `poll_junk` (4397-4409) and `_scan_giveup` (3385-3434)

**What happens.**
- `rspamd_scan_detail` sends the message's From address as the HTTP `From` header.
- With a bare `bayes_user` (the live `bytelord`), it sends the first To/Cc address from `first_recipient` as `Rcpt`.
- Both are parsed from the message with `email.policy.compat32`. Raw 8-bit bytes in an address come back as `U+FFFD` or surrogate escapes.
- `http.client` encodes header values as Latin-1, so the POST raises `UnicodeEncodeError`.
- `rspamd_scan_detail` catches it as `ValueError` and returns `None`, the same result as "rspamd is down".

**Consequences.**
- The Inbox scan (and `poll_junk`) **halts at that UID** (`halted = True; break`). Nothing newer is scored until the poison give-up: 5 failed passes over at least 10 minutes, plus two healthy probes.
- The give-up then leaves the message **unscored in place** (`scan_giveup`).
- In move mode that is a **filter bypass** for the message itself, and **a 10-minute stall for every message behind it**. One such message every 10 minutes keeps the mailbox unfiltered.
- The rspamd health probe uses clean headers, so it always passes.

**Reproduction** (Python 3.12 container, local HTTP stub standing in for rspamd):

```text
8bit From addr      env=('m1@b.com', 'hello', 'j��rg@example.de')  scan=FAIL(None)
8bit To addr        rcpt='r��ch@bytecave.net'                      scan=FAIL(None)
8bit Cc first       rcpt='��@x.com'                                scan=FAIL(None)
8bit From display   (address is ASCII)                              scan=OK
8bit Subject                                                        scan=OK
```

**Live exposure.** Exchange usually re-encodes headers as RFC 2047 when serving IMAP, and the live database has no `scan_failed` events in 7 days. Dovecot and most other IMAP servers serve the raw bytes, and EAI (RFC 6532) mail legitimately carries UTF-8 addresses.

**Trace.**
- Requirement: every new Inbox message is scored. CR-010 says spam "cannot bypass the filter" by omitting a header.
- Architecture: slice 6 (HTTP From/Rcpt metadata) and OPUS-CR-002 (poison give-up).
- Implementation: the headers are not sanitised.
- Tests: `test_rspamd_scan.py` covers the From/Rcpt choice with ASCII only.

**Fix.**
- `first_recipient` skips addresses that are not HTTP-safe and falls back to the mailbox.
- `rspamd_scan_detail` omits `From` when it is not printable ASCII, and never sends an unsafe `Rcpt` (it uses the identity when that is an address, otherwise omits it).
- rspamd still parses the MIME From/To itself, so no scoring input is lost. The CR-019 policy (Rcpt = first To/Cc) is unchanged for normal mail.

---

## Medium

### FABLE-CR-004 — rspamd stops evaluating rules once a score passes `reject`

**Severity:** Medium · **Reachable:** scoring on every account; the decisions happen in flag/move · **Status:** verified in rspamd 4.2.0 source
**Files:** `filter/filter.py` `rspamd_scan_detail` (no `Pass` header); `rspamd/local.d/actions.conf` ("Cosmetic labels only")

**What happens.**
- rspamd's symbol cache (`symcache_runtime.cxx` `check_process_status`) returns `limit_reached` as soon as the running score exceeds the required score: the highest action with a threshold, here `reject = 15` (`rspamd_task_get_required_score`).
- `should_skip` then skips every FILTER rule that has not started yet: DMARC, fuzzy, RBL/URL rules and negative-score allow rules.
- Only a request with `Pass: all` (`protocol.c`, `RSPAMD_TASK_FLAG_PASS_ALL`) runs everything, and the filter never sends it.

So `reject` is not cosmetic. A message that crosses 15 partway through is scored on a truncated, order-dependent set of symbols. That is typical here: `BROKEN_HEADERS` +8 plus auth-recheck symbols. `apply_m365_auth_trust` then subtracts the bucket-B weight from that truncated total. The ≥8 Junk and <4 rescue decisions, and the `score_detail` shown on the dashboard, rest on a partial evaluation. Live: 45 of the 4,775 messages scored in the last 7 days are still ≥15 after bucket B.

**Fix.** Send `Pass: all` on `/checkv2`. The production scan, the health probe and `explain_score.py` share the function. Scores of high-scoring mail can move once this is deployed, so watch the dashboard after the rebuild.

### FABLE-CR-005 — Unbound forwards every lookup to Cloudflare, so the DNS blocklists refuse the queries

**Severity:** Medium · **Reachable:** scoring on every account · **Status:** verified live (read-only) · **Operator decision, not changed**
**Files:** `deploy/bytelord-compose.yaml` and `docker-compose.yml` (`mvance/unbound:1.22.0`, no config mounted); `rspamd/local.d/options.inc` ("recursive DNS so DNSBL lookups are not rate-limited… Critical for Spamhaus")

**What happens.**
- The image's default `forward-records.conf` forwards `"."` over DNS-over-TLS to `1.1.1.1` and `1.0.0.1` (checked in the running container). Unbound is not recursing.
- Spamhaus, URIBL and SURBL refuse queries that arrive through public resolvers, so rspamd records `*_BLOCKED*` symbols at weight 0. **237 of 370** messages scored in the last 3 days carry one.
- The stock Spamhaus ZEN/DBL and URI blocklist rules therefore contribute nothing, and every sender and URL domain is sent to Cloudflare.
- The explain-high-scores plan (2026-09-06) already noticed `RBL_SPAMHAUS_BLOCKED_OPENRESOLVER` and `URIBL_BLOCKED`.

**Why it was not changed here.** Turning on real recursion makes those blocklists start scoring on a live move-mode mailbox. That is a scoring change the operator should schedule and watch. Spamhaus also refuses some VPS ranges even for direct recursion; those then need a free Spamhaus DQS key.

**Recommendation.**
1. Mount a comment-only (recursion-only) `forward-records.conf` read-only at `/opt/unbound/etc/unbound/forward-records.conf` in both compose files.
2. Recreate Unbound, then **restart rspamd**, which resolves `spamfilter-unbound` once at start.
3. Check with `docker exec spamfilter-unbound drill 2.0.0.127.zen.spamhaus.org @127.0.0.1`. A `127.0.0.x` answer means Spamhaus is answering.
4. Watch the `*_BLOCKED*` symbols disappear from `score_detail`.

### FABLE-CR-006 — List editor: Cancel after a 400 saves the text into a different list

**Severity:** Medium (the consequence is severe; the trigger is a specific sequence) · **Reachable:** dashboard (admin) · **Status:** verified by code path
**Files:** `filter/lists.js` 66-85; `filter/dashboard.py` `_list_page` (1847-1932) and `_list_page_render` (1935-1983); `filter/filter.py` `Db.list_replace` (1897-1949)

**What happens.**
- The "revert on Cancel" logic for the kind radios and the scope dropdown reads `window.location.search`.
- A validation error (400) is rendered as the response to `POST /lists/domains` or `POST /lists/users`, so the URL has **no query string**, even though the page shows the posted scope and kind. The fallback is then `"allow"` and the first dropdown option.

**Example.**
1. The admin edits `rjmetalfab.com` → Block (which holds `@kickstarlaunch.com`) and saves a typo, which returns 400.
2. They click "Allow", then Cancel at "Leave anyway?". Allow stays selected.
3. They fix the typo and click Save.
4. The POST carries `kind=allow` with the block text. `list_replace` deletes the whole allow list, inserts the block entries as **allow**, and deletes them from block.

The dropdown has the same problem: the text lands on the first roster domain.

**Why the server cannot tell.** The form posts `scope`/`kind` from the navigation controls, not from what was loaded into the textarea.

**Relation to prior findings.** OPUS-CR-026 (Cancel restores the rejected text) is related, but this failure writes to the wrong list.

**Fix.**
- The visible controls become navigation only (renamed so they are not submitted).
- Hidden `scope`/`kind` fields carry the list that was loaded.
- Cancel reverts to those loaded values.
- A Save always writes back to the list the text came from.

### FABLE-CR-007 — A stale list page reverts Outlook Allowlist/Blocklist drags

**Severity:** Medium (the impact is real; the likelihood is low) · **Reachable:** dashboard (admin) plus any user's IMAP drag · **Status:** verified by code path
**Files:** `filter/dashboard.py` `_list_page` (1895-1922); `filter/filter.py` `Db.list_replace` (1897-1949) and `_drain_list_folder` → `list_flip_address` (5158)

**What happens.** `list_replace` deletes every row of (scope, kind) and re-inserts the textarea text. It then deletes each pattern from the sibling kind. There is no check that the list is unchanged since the page was loaded, and a page can stay open for the 8-hour idle session.

**Example.**
1. An admin opens User lists → "Steve Jones" → Allow, which contains `promo@shop.com`.
2. Steve drags a promo mail into Blocklist. The drain flips `promo@shop.com` to block and learns spam.
3. The admin saves an unrelated edit from the stale page.
4. `promo@shop.com` is back on allow and Steve's block row is deleted as a "sibling".

Allowlist drags made during that window are wiped outright. The only trace is `flipped=N` in the `list_dashboard_save` event.

**Trace.**
- Slice 12 says IMAP drags "persist immediately" and the dashboard is "the only batched editor".
- Nothing in slice 12 covers the two writers racing.
- Tests: none.

**Fix.**
- Render a hidden `snapshot`: a SHA-256 over scope type, scope key, kind, and both kinds' patterns.
- Inside the `BEGIN IMMEDIATE` transaction, recompute it and compare. On a mismatch, return 409, write nothing, and show the posted text with a "changed since you opened it" message and a fresh snapshot, so a second Save is a deliberate overwrite.
- A 400 re-render carries the *posted* snapshot forward.

### FABLE-CR-008 — Oversize Train-Ham never gets its Inbox copy

**Severity:** Medium · **Reachable:** every mode (retention is on in flag/move, and live for `rich_bytecave`) · **Status:** verified by code path
**Files:** `filter/filter.py` `_restore_train_ham_to_inbox` (4844-4848: `if oversize: continue`), `_drain_train_folder` (4664-4672: the oversize branch runs before the ham "restored" gate at 4684-4693), `retention_sweep` (5259-5267)

**What happens.**
- The README and the 2026-09-28 policy say a message dropped in Train-Ham is copied to the Inbox, learned, then archived in Trained-Ham.
- For a message over `MAX_FETCH_BYTES` (5 MiB), the restore skips it. The drain's oversize branch then MOVEs it to Trained-Ham unlearned, **with no Inbox copy**.
- A COPY failure has the same result for oversize mail, because the oversize branch ignores the "restored" gate.
- Retention later moves Trained-Ham to Deleted Items. Retention uses the original delivery date (`SEARCH BEFORE`), so a ham message delivered more than 8 days earlier goes on the next hourly sweep.

Legitimate mail with large attachments (an invoice PDF junked by Microsoft) is the typical oversize case, so the user's rescue attempt ends with the mail in Deleted Items.

**Trace.**
- Requirement: README "Folder-based training" and the Train-Ham policy row in IMPLEMENTATION_STATUS.
- Plan: `train-ham_inbox_hold` ("COPY new Train-Ham messages to Inbox before try_learn").
- Implementation: the oversize branch was not updated.
- Tests: none for oversize ham.

**Fix.**
- The restore COPYs oversize Train-Ham server-side. It needs no body download, and without a body there is no identity check.
- It records `inbox_copied` on a metadata-only row.
- The drain applies the "restored" gate to oversize ham too.

`scan_inbox` already skips oversize mail, so the Inbox copy is never scored or moved.

### FABLE-CR-009 — A Train-Ham copy of Junk mail is treated as a user revert

**Severity:** Medium · **Reachable:** every mode; the block-list consequence is move/flag · **Status:** reproduced (current code, Python 3.12, imapclient 3.1.0)
**Files:** `filter/filter.py` `_restore_train_ham_to_inbox` (4888, 4913-4929), `_scan_inbox_uid_batch` (revert branch 3742-3783 runs before the list check and the restore hold at 3815-3862); `filter/test_shadow_mode.py` `RecordingIMAP.copy`; `filter/test_core_review_fixes.py` `test_train_ham_copies_to_inbox_then_archives`

**Two defects that combine.**

1. **The UIDPLUS mark is dead code.**
   - For `UID COPY`, imapclient 3.1.0's `copy()` returns **`None`**: `imaplib.uid()` returns the untagged FETCH data and drops the tagged `[COPYUID …]` text. This was checked against a fake server that sends `OK [COPYUID 38505 7,9:10 1001:1003]`.
   - So `isinstance(copied, dict)` is always false. The new Inbox row is never pre-marked `ham_restored`, although the status docs say it is "when UIDPLUS returns the new UID".
   - The unit test passes only because `RecordingIMAP.copy` returns a `{src: dest}` dict.
2. **The revert branch wins.** Without that mark, `scan_inbox` sees the Inbox copy with `prior is None`. When the same body also has a Junk row (the usual case: the user drags a mis-junked message from Junk to Train-Ham), it takes the **Junk → Inbox user-revert** branch and `continue`s. The copy is then:
   - **never scored** (and never picked up by catch-up);
   - **never list-checked.** A block-list hit does *not* send it to Junk, contradicting the policy "a block-list hit still forces Junk";
   - queued for a **second ham learn** (208 from rspamd, but one more slot of the hourly budget and a duplicate `learn_ham` event);
   - recorded with `our_action` NULL, not `ham_restored`.

**Reproduction** (current code, scratch pytest; Junk row + `inbox_copied` Train-Ham row + person block on the sender, move mode, score 9):

```text
RESULT our_action=None score=None pending_learn='ham' pending_moves=0
```

The expected result is a stored score, `pending_move` (block-list hit), and no pending learn.

**Live data.** Every live `inbox_copied` row so far came from mail *without* a Junk row, so the hold went through the body-SHA sibling path as intended. This case has not happened on ByteLord yet.

**Trace.**
- Plan: `train-ham_inbox_hold` ("store the score… A block-list hit still forces Junk").
- Policy: the IMPLEMENTATION_STATUS "Train-Ham restore" row.
- Implementation: branch order in `_scan_inbox_uid_batch`, plus the dead UIDPLUS branch.
- Tests: `test_train_ham_restore_blocklist_still_junks` has no Junk sibling, so it cannot see this. The fake `copy()` does not match imapclient.

**Fix.**
- Recognise a Train-Ham restore (by body SHA against an `inbox_copied` row) *before* the revert branch, so a restore is never a revert. It then gets scored, list-checked (block → Junk) and held (`ham_restored`) as documented.
- Remove the unreachable UIDPLUS branch; the SHA hold already covers every server.
- Make the fake `copy()` return `None` like imapclient, and correct the docs.

### FABLE-CR-010 — The locally built filter image carries the upstream author's registry name

**Severity:** Medium · **Reachable:** deploy · **Status:** verified in the compose file and docs
**Files:** `deploy/bytelord-compose.yaml` (`build:` plus `image: ghcr.io/marcelverdult/imap-spamfilter:latest`)

**What happens.**
- The fork's image is built locally but tagged with the upstream repository's public GHCR name.
- `docker compose pull` (without `--ignore-buildable`), or `up --pull always`, fetches upstream's image under that tag.
- The next `up` then runs upstream code against the live `accounts.yml`, the secrets mount and the SQLite database. That code has none of this fork's lists, Train-Ham restore, auth trust or review fixes.
- The deploy runbooks already use `pull` (for rspamd), so one missing service name is enough.

This is about who owns the name, not the accepted pinning risk (ChatGPT CR-016).

**Fix.** Tag the local build with a name no registry can serve (`imap-spamfilter:bytelord`). Third parties cannot publish a Docker Hub `library/` name.

### FABLE-CR-011 — Microsoft Authentication-Results trust applies to every account

**Severity:** Medium, latent: every live account is M365 today · **Reachable:** scoring and rescue on any account · **Status:** verified by code path · **Recommendation, not changed**
**Files:** `filter/filter.py` `_trusted_m365_ar` (387-414), `apply_m365_auth_trust` (468-502), `m365_spoof_verdict` (435-455)

**What happens.** Bucket B trusts the *outermost* `Authentication-Results` header when its authserv-id is `mx.microsoft.com`. That is only safe when Microsoft is guaranteed to have prepended that header. The check is global, not per account. If a non-M365 mailbox is added (the status file mentions possible future `bytelord.net` IMAP accounts), any sender can put `Authentication-Results: mx.microsoft.com; spf=pass; dkim=pass; dmarc=pass; compauth=pass` at the top of their message. That would:
- zero `R_DKIM_REJECT`, `R_SPF_FAIL`, `DMARC_POLICY_*` and `BLACKLIST_DMARC`;
- make `m365_spoof_verdict` return False, so an allowlist-spoofing message is rescued and not flagged.

**Recommendation.** Add a per-account `m365_auth_trust` (default `true` to keep today's behaviour) and require `false` for any account whose `imap_host` is not the M365 proxy. Document it next to bucket B. This was left as a recommendation because it is a configuration-surface change.

---

## Low

### FABLE-CR-012 — An 8-bit Message-ID erases Subject and From too

**Files:** `filter/filter.py` `parse_envelope` (2748-2761) · **Reachable:** every mode · **Status:** reproduced

With `compat32`, `msg.get()` returns an `email.header.Header` object, not a `str`, when the value has raw 8-bit bytes. `msgid.strip()` then raises `AttributeError`. The blanket `except` returns `(None, "", "")`, which discards Subject and From as well. Knock-on effects:
- An Allowlist/Blocklist drag of such a message records **no list entry** ("empty From").
- The dashboard shows a blank row.

Reproduced: `8bit Message-ID env=(None, '', '')`.

**Fix.** `str(msg.get(...) or "")` for each header. This must ship together with FABLE-CR-002, because the Message-ID would then contain `U+FFFD` and reach the SEARCH path.

### FABLE-CR-013 — A YAML syntax error prints the offending `accounts.yml` line, password included

**Severity:** Low (ByteLord uses the dummy proxy password; generic installs hold real ones) · **Status:** verified
**Files:**
- `filter/filter.py` `load_accounts` (1005) and `yaml_max_list_entries` (1204) call `yaml.safe_load` without catching `yaml.YAMLError`.
- `main()` catches only `ConfigError`.
- The dashboard logs the exception text on every page (`_configured_account_names`).
- Both CLIs print tracebacks.

PyYAML's `MarkedYAMLError` text quotes the source line with a caret. So `password: "S3cret` (unclosed quote) or `password: S3cret: x` puts the password into `docker logs` and the dashboard log.

**Fix.** Map `yaml.YAMLError` to `ConfigError` with only the line and column, and map `OSError` to `ConfigError`.

### FABLE-CR-014 — The user-add helper grants admin to a blank scope

**Files:** `filter/dashboard.py` `__main__` (2139-2154) · **Reachable:** the operator CLI

At the scope prompt, typing `,`, `|` or `, ,` gives an empty account list. The unknown-account check passes vacuously, and `scope = "|".join(wanted) or "admin"` writes the user as **admin**. That fails open, and it contradicts the README ("a typo cannot silently bind a user to nothing"): here the user is bound to everything.

**Fix.** An empty list exits with an error; only an explicit `admin`, or Enter for the documented default, produces admin.

### FABLE-CR-015 — A non-ASCII CSRF token returns 500

**Files:** `filter/dashboard.py` `_check_csrf` (517-521) · **Reachable:** dashboard (admin session only)

`hmac.compare_digest(str, str)` raises `TypeError` for non-ASCII strings, and nothing catches it, so the response is a 500 with a traceback. Nothing is written.

**Fix.** Compare UTF-8 bytes.

### FABLE-CR-016 — The users file is rewritten in place

**Files:** `filter/dashboard.py` `_write_private` (169-180) · **Reachable:** the operator CLI

The file is opened with `O_TRUNC` and written with a single unchecked `os.write`, with no fsync. The dashboard re-reads it on every authenticated request, so:
- a request that lands between the truncate and the write logs the user out;
- a short write or `ENOSPC` leaves an empty or partial file, which locks everyone out and stops the dashboard at the next restart.

**Fix.** Write a temp file in the same directory (0600, `O_EXCL`), loop until every byte is written, fsync, `os.replace`, then fsync the directory.

### FABLE-CR-017 — A bad users-file byte returns 500 instead of failing closed

**Files:** `filter/dashboard.py` `_load_users` (236-244), `_verify_pbkdf2` (153-163)

- `read_text()` raises `UnicodeDecodeError`, a `ValueError` that is not caught, for a users file saved in another encoding, so every page returns 500.
- A non-ASCII hash field makes `compare_digest` raise `TypeError`.

Nobody gets in either way, but the operator gets a crash, not a logged warning.

**Fix.** Catch `UnicodeDecodeError`, log it and treat the file as empty. Add `TypeError` to `_verify_pbkdf2`'s except clause.

### FABLE-CR-018 — The error caret is off by one after a leading blank line

**Files:** `filter/dashboard.py` 1978

The HTML parser drops a newline that immediately follows `<textarea>`. `parse_list_text` numbers lines on the raw posted text, so for `"\nbad x"` the error says line 2 while the browser shows the bad text on line 1.

**Fix.** Always emit a newline after the start tag.

### FABLE-CR-019 — The rspamd stats fetch would forward the controller password on a redirect

**Files:** `filter/dashboard.py` `_rspamd_stats` (1002-1017)

`requests.get` follows redirects and re-sends custom headers such as `Password` to the new host. It strips only `Authorization`. The controller is internal, so the risk is low.

**Fix.** `allow_redirects=False`.

### FABLE-CR-020 — `bootstrap_train.py` exit codes and README folder paths

**Files:** `filter/bootstrap_train.py` (150-154, 174-178, 314-322, 364-365); README "Bootstrap training (b)"

- **A missing source folder exits 0.** Single-account mode prints `skip Train-Spam: …` and exits 0 when the source folder does not exist. The README examples use `Train-Spam` / `Trained-Spam`, but the real folders are `Junk/Train-Spam` (`Junk Email/Train-Spam` on M365), and single-account mode does no SPECIAL-USE remap. The documented command therefore silently does nothing and "succeeds".
- **Oversize mail fails the re-feed.** `--all-trained` counts a message over 5 MiB as `failed`, but production deliberately archives oversize Train-* mail into Trained-* unlearned. Every re-feed of a folder holding one such message exits 1, which masks real failures.

**Fix.**
- A missing source folder exits 1.
- Oversize counts as skipped in the read-only re-feed.
- The README uses full folder paths.

### FABLE-CR-021 — The README points `bootstrap_train.py` at the live Train-Ham folder

**Files:** README "Bootstrap training (b)"; `filter/bootstrap_train.py` `train_folder`

The README example `bootstrap_train.py your_name Train-Ham ham --move-to Trained-Ham` learns and MOVEs directly. That:
- bypasses the 2026-09-28 Train-Ham Inbox restore;
- races the live drain on the same folder;
- leaves ham the user wanted back only in Trained-Ham, where 7-day retention moves it to Deleted Items.

**Fix.** Document a dedicated bulk folder for the CLI, never the live Train-* folders. A refusal inside the tool for the live folders is recommended, not added.

### FABLE-CR-022 — `explain_score.py --message-id` can explain the wrong message

**Files:** `filter/explain_score.py` (85-86, 41-55, 124-127)

The database lookup returns `(folder, uid)` and drops `uidvalidity`, and `_fetch_body` ignores the UIDVALIDITY the SELECT reports. So:
- after a UIDVALIDITY reset the same UID can be a different message, and the tool explains that one instead;
- a UID that has since moved away is reported as "exceeds the 5 MiB fetch cap (size=None)".

**Fix.** Carry and compare the uidvalidity (exit 2 on a mismatch), and tell "no FETCH row" apart from oversize.

### FABLE-CR-023 — List-folder drains log a `list_imap_add` event every pass

**Files:** `filter/filter.py` `_drain_list_folder` (5152-5189)

When the learn is deferred (hourly budget spent, safe mode, rspamd error), the message stays in the list folder. Every pass (30 s live) re-downloads it and logs another `list_imap_add ... outcome=exists` event, which floods the Events page. Unlike the Train-* drains (CR-013), there is no budget check before the body FETCH.

**Fix.** Log `list_imap_add` only for `inserted`, and skip the drain when the learn budget is spent, as Train-* does.

### FABLE-CR-024 — Generic compose and `.gitignore` gaps

- `docker-compose.yml` (the generic path) has no `stop_grace_period`. ByteLord's has 90 s, for the stated reason that 10 s can cut a MOVE off before its database update is written.
- `.gitignore` does not cover `secrets/`. Bootstrap's default secrets path is `$APP/secrets/imap-spamfilter.env`, so with `SPAMFILTER_APP` pointing at the checkout, `git add -A` would stage it. `.bootstrap.version` and `.bootstrap-stage.*/` are not covered either.

### FABLE-CR-025 — Dashboard tests that pass with the check removed

**Files:** `filter/test_dashboard.py`

- `test_real_waitress_accepts_large_list_post_but_not_large_login` posts to `/lists/users` **anonymously**. `_requires_admin` redirects before the body is read, so the test passes even if the 256 KiB `_list_post_size` hook is deleted.
- CSRF is only tested with *no* session token, never with a wrong one.
- There is no escaping test on `/messages`, where subject, sender and `score_detail` symbols are attacker-controlled.
- The `list_dashboard_save` event and POST `/logout` are never asserted.

See [Recommended new tests](#recommended-new-tests).

### FABLE-CR-026 — The orientation test command fails three tests

**Files:** `code_review_orientation.md` "How to run tests"

The documented command mounts only `filter/`. Three tests read `README.md` and `unraid/` from the repository root, so it reports `3 failed, 426 passed`. The IMPLEMENTATION_STATUS command, which mounts the whole repository, gives `429 passed`.

**Fix.** Use the whole-repository mount in the docs.

### FABLE-CR-027 — Stale "hard rules" and "read-only" wording

- `filter/filter.py`'s module docstring says the only EXPUNGE is for list-folder leftovers. Since `0b982e9`, Train-* leftovers are expunged too.
- `main()` still says "Optional read-only dashboard", as do the Dockerfile and Unraid template comments. Admins can now edit lists.
- `accounts.yml.example` and the README mode table say shadow means "No writes to Inbox, Junk, or Trash". In shadow the filter still COPYs Train-Ham to the Inbox, MOVEs list drains to Inbox/Junk, and sets `\Flagged` via `flag_untrained_junk`, all by later decision.
- The README "Persistent data" section and slice 7 §6 describe Bayes `expire = 0` as "never expire" (FABLE-CR-001).

---

## Info and operator recommendations

### FABLE-CR-028 — Oversize mail bypasses allow/block routing

A message over 5 MiB is never scored *or list-checked* (`_scan_inbox_uid_batch` 3707-3712), so a blocked sender who pads a message past 5 MiB stays in the Inbox. This is documented as oversize behaviour, but list routing only needs headers.

**Recommendation.** FETCH `BODY.PEEK[HEADER]` for oversize UIDs and apply block/allow routing, still with no score.

### FABLE-CR-029 — Expected token-expiry reconnects dominate `conn_error`

The live events table has **1,561 `conn_error` "idle_done failed"** rows in 7 days across ten accounts (about 22 per account per day), plus `AccessTokenExpired` BYEs. This is Microsoft invalidating the IMAP session when the proxy's access token expires. The loop recovers correctly and loses no mail, but the noise hides real connection problems.

**Recommendation.** Classify a `BYE`/`AccessTokenExpired`/`idle_done` failure right after a long IDLE as `conn_recycled` rather than `conn_error`.

### FABLE-CR-030 — Dashboard session cookie is not `Secure` in production

`DASHBOARD_COOKIE_SECURE` is unset in the running container (checked), and not set in `deploy/bytelord-compose.yaml`, although the site is `https://spam.bytelord.net`. The risk is low on NetBird.

**Recommendation.** Set `DASHBOARD_COOKIE_SECURE: "1"` in the ByteLord compose. Browsers treat `http://127.0.0.1` SSH-tunnel access as a secure context, so the tunnel keeps working. This is a deploy change and was left for the operator.

### FABLE-CR-031 — Other dashboard observations

- **Lockouts refuse a correct password.** The per-user and global login lockouts reject even a correct password, so any NetBird peer can keep a named admin locked out with about 10 bad attempts a minute. Consider letting a correct password through when its own (IP, user) pair is not locked.
- **`admin` as an account name.** The scope string `admin` is compared case-insensitively, so an account *named* `admin` could never be given a scoped user. Consider reserving the name in `load_accounts`.
- **Query parameters on POST.** `request.values` lets query parameters override form fields on list POSTs. FABLE-CR-006's fix makes the loaded scope/kind explicit hidden fields.
- **Cache lifetimes on static files.** `Cache-Control: no-store` overrides the `max_age` given for `favicon.png` and `lists.js`. The comment about a short TTL is misleading.

### FABLE-CR-032 — rspamd, CI and image hardening recommendations

- **`allow_file_and_shm_inputs`** defaults to true in 4.2.0 for both workers, and a password does not gate it. The normal worker `*:11333` is reachable without authentication from every `spamnet` container, including email-oauth2-proxy, and the filter never uses file inputs. Set it to `false` in `worker-normal.inc` and `worker-controller.inc.template`.
- **Controller passwords.** `enable_password == password` in `worker-controller.inc.template`, so every WebUI login has admin rights. Consider a separate enable password.
- **`rbl.conf`.** It registers `RBL_SPAMHAUS_ZEN`, `RBL_ABUSIX` and `RBL_SPAMCOP` with no weight (score 0), duplicates the stock Spamhaus rule, and Abusix needs an account key. The file's comment calls it "the working RBL path". Remove these rules or weight them deliberately, after FABLE-CR-005.
- **Healthcheck.** The Docker `HEALTHCHECK` proves only the main thread's heartbeat, which `main()` rewrites every 60 s even when every account is in reconnect backoff (the README already notes this). The per-account `account_heartbeat` table could back a real check.
- **CI token scope.** `permissions: packages: write` is workflow-wide in `build.yml`; scope it to the publishing job.
- **Image ownership.** The `Dockerfile` runs `chown -R 99:100 /app`, which lets the runtime uid rewrite its own code on the generic path. Keep code root-owned.
- **Learn cache 404.** rspamd can answer **404** "already learned" from its pre-learn cache (`redis_cache.cxx`), and `rspamd_learn` maps 404 to `error`, so that message is retried with backoff forever. This comes from the ops subagent's source reading and was not reproduced. Consider mapping 404 to `already`.

---

## Prior findings that are still open

| Prior ID | Status now | Note |
|---|---|---|
| OPUS-CR-008 | Partly fixed | Rescue still reverses a user's drag into Junk from a server-rule folder when the mail is under 3 days old: no Inbox sibling, and a fresh INTERNALDATE. Consider requiring `\Seen` to be absent at first sight. Provider Junk is almost always unseen when the 30 s poll finds it; a user-moved message is usually read. Policy change, so recommended only. |
| OPUS-CR-014 | Open (operator) | Inbox→Junk score moves and rescues use a plain MOVE. **New evidence:** the live retention sweeps moved 500, 500, then 120 Trained-Ham to Deleted Items and then nothing, so Exchange did *not* leave copies for that retention MOVE. |
| OPUS-CR-019 | Open (operator) | `Rcpt` = first To/Cc. FABLE-CR-003 only changes the case where that address is not HTTP-safe. |
| OPUS-CR-020 | Open (accepted) | Allow/Block drags learn twice. One more side effect: an Allowlist drag from Junk leaves the Inbox copy **unscored**, with `our_action` NULL rather than `allowlisted` (the revert branch `continue`s before the list check). The Train-Ham variant of the same problem is FABLE-CR-009. |
| OPUS-CR-021 | Open | The list parser still accepts one-word hosts. **New consequence** since subdomain matching (`8d310b9`): a line `com` is stored as `@com` and matches **every `.com` sender**. |
| OPUS-CR-022 (`env_file`) | Open (kept by decision) | **New angle:** Compose's `env_file` parser expands `$VAR`, strips an unquoted ` #…` and processes escapes. Bootstrap's parser, which renders `worker-controller.inc`, does none of that, and `_load_secret` prefers the environment. A password containing `$`, ` #` or `\` would reach the filter and rspamd differently, and every learn would then fail with 401/403. The recommendation is unchanged: drop `env_file` once the secrets file is confirmed to hold only the RSPAMD/REDIS keys. |
| OPUS-CR-023 | Open (operator) | `DASHBOARD_TRUSTED_PROXIES` is unset in the running container (checked). |

---

## Traceability: requirement → architecture → acceptance → code → tests

| Requirement (source) | Architecture decision | Slice acceptance criterion | Implementation | Test coverage today | Finding |
|---|---|---|---|---|---|
| Bayes tokens are kept; learning happens only from explicit teaching (README "Persistent data", slice 9) | One shared notebook, no autolearn, `noeviction` Redis | Slice 7 §6 "Bayes `expire = 0`" (meant: never expire) | `classifier-bayes.conf` `expire = 0` turns rspamd's expiry on with a zero TTL | No test reads the rspamd config | FABLE-CR-001 |
| rspamd actions are cosmetic; the filter decides from the raw score (`actions.conf`, 2026-09-24 policy) | Scan with `/checkv2`, then apply bucket B | Thresholds 8 / 4 come from `accounts.yml` | No `Pass: all`, so rspamd stops at `reject = 15` | No header test | FABLE-CR-004 |
| DNSBLs work through local recursion (`options.inc`, README architecture) | In-stack Unbound | — | The image forwards to Cloudflare | None (deploy) | FABLE-CR-005 |
| Fail closed; 24/7 per-account loop (README hard rules) | Message-ID is metadata, not identity (slice 5); identity is the body SHA (OPUS-CR-001) | A list drain never deletes on a Message-ID match alone | `_identical_copies_in_folder` verifies the SHA but sends the raw Message-ID to SEARCH | Collision test only (ASCII) | FABLE-CR-002 |
| Every new Inbox UID is scored, including mail without a Message-ID (OPUS-CR-010) | HTTP `From`/`Rcpt` metadata, no fake `Ip`/`Helo` (slice 6) | Scan uses the message From; CR-019 Rcpt | `rspamd_scan_detail` / `first_recipient` send unsanitised headers | ASCII-only `test_rspamd_scan.py` | FABLE-CR-003 |
| Poison messages must not halt scanning forever (OPUS-CR-002) | Give-up after 5 passes, ≥10 min, 2 healthy probes | An outage never gives up; poison does | `_scan_giveup` | Covered for no-body/scan-failed | FABLE-CR-003 (a whole class of input is poison) |
| Train-Ham returns the mail to the Inbox (2026-09-28 policy) | COPY before learn, fingerprint hold in `scan_inbox` | "COPY new Train-Ham messages to Inbox before try_learn" | Oversize skipped; UIDPLUS branch never runs; the revert branch pre-empts the hold for Junk-origin mail | Fake `copy()` returns a dict; no oversize or Junk-origin test | FABLE-CR-008, FABLE-CR-009 |
| Dashboard Save replaces exactly one (scope, kind) list (slice 12) | Server-side parse, one transaction, sibling delete | Invalid line → 400 and no writes | Scope/kind taken from navigation controls; no concurrency check | 400/no-write tested; no wrong-list or stale-save test | FABLE-CR-006, FABLE-CR-007 |
| Bucket B trusts only Microsoft's own AR (2026-09-23 lock) | Outermost AR from `mx.microsoft.com` | Auth-fail and non-Microsoft AR keep the weight | Trust is global, not per account | Covered for M365 shapes | FABLE-CR-011 |
| Dashboard users fail closed (README, slice 8) | Missing scope → line ignored | The helper rejects unknown accounts | An empty scope becomes admin | Helper untested | FABLE-CR-014 |

## Recommended new tests

These are tests that do not exist today. Items 1–9 landed with the fixes; [`CLAUDE_FABLE5.1_CODE_FIXED.md`](CLAUDE_FABLE5.1_CODE_FIXED.md) maps each one to its commit. Item 10 is a recommendation only.

1. `_identical_copies_in_folder` with Message-IDs `abc{5}`, `a\r\n b@x`, `a"b`, `jörg@x` (via `U+FFFD`): no SEARCH is sent, the result is empty, and nothing raises. A Train-Spam leftover with such an ID is kept, not expunged, and the drain returns normally.
2. `rspamd_scan_detail` with a raw 8-bit To, Cc and From. Capture the headers passed to `requests.post` and assert every value is printable ASCII. `first_recipient` skips the unsafe address and falls back to the mailbox. An end-to-end check against a local HTTP stub returns a score.
3. `parse_envelope` keeps Subject and From when only the Message-ID has 8-bit bytes.
4. Oversize Train-Ham: COPY to the Inbox, then MOVE to Trained-Ham. If the COPY fails, the message stays in Train-Ham.
5. A Train-Ham restore whose body also has a Junk row: `scan_inbox` stores the score and marks `ham_restored` with no pending learn. With a person block on the sender, it queues `pending_move`. `RecordingIMAP.copy` returns `None`, as imapclient 3.1.0 does for UID COPY.
6. Dashboard: a POST whose hidden scope/kind differ from the navigation controls writes to the loaded list; a stale snapshot returns 409 and writes nothing; a wrong CSRF token (ASCII and non-ASCII) returns 400; a leading blank line keeps the caret line; an authenticated 1,000-line Save over 16 KiB succeeds.
7. User-add helper scope parsing: `","`, `"|"` and `" , "` exit with an error.
8. A config test that fails when `rspamd/local.d/classifier-bayes.conf` sets `expire` to a non-negative number (FABLE-CR-001); a `Pass: all` header assertion (FABLE-CR-004); invalid YAML containing a sentinel password raises `ConfigError` whose text does not contain it (FABLE-CR-013); `bootstrap_train.py` exits non-zero for a missing source folder and zero when the only skip is an oversize Trained-* message (FABLE-CR-020).
9. *(Added in the fix pass after all, commit `1473c14`.)* A `/messages` escaping test with `<script>` in the subject, `"><img>` in the sender and `<b>` in a `score_detail` symbol; a POST `/logout` test; a `list_dashboard_save` event assertion.
10. **Not added (recommended):** a scan-to-move test in move mode that runs `drain_train_ham → scan_inbox → execute_due_moves` in one pass with the real imapclient return types, to prove the hold end to end.

## Controls checked and found correct

- **Shadow write policy.** Inbox is EXAMINEd in shadow. Retention is gated by `mode_allows_retention`. `poll_junk` examines read-only, and its only write is the opt-in untrained-Junk flag, a documented 2026-09-28 decision.
- **Bookmarks.**
  - The first sight of a folder records the max UID.
  - The advance is a prefix of terminally handled UIDs.
  - The `n:*` edge case is filtered locally.
  - A UIDVALIDITY change clears pending moves, the bookmark and pending learns.
- **Learning.**
  - List-contradiction skips are terminal and do not set `learned_as`.
  - The flip-flop cooldown and the unlearnable retry are per IMAP object.
  - The learn budget is checked before body FETCHes for Train-* and pending learns.
  - `Learn-Type: bulk` is sent.
  - The Bayes identity is the same `Delivered-To` prefix on scan and learn.
- **Rescue.** Spoofed and old mail is refused, blocklisted mail is never rescued, and the score is re-checked at execution time with the same `rescue_below`.
- **Moves.** The move quota is subtracted before a batch, and allowlisted mail is re-checked before `execute_due_moves` MOVEs it.
- **SQLite.** `BEGIN IMMEDIATE`, the whitelisted UPDATE columns, bounded fingerprints and pruning are all correct.
- **rspamd / ops** (subagent, checked against the 4.2.0 sources):
  - Scan and learn use the same `Delivered-To` identity, and rspamd keys Bayes on the first `Delivered-To`.
  - `hfilter_group.conf` zeroes exactly `HFILTER_HOSTNAME_UNKNOWN` and `RDNS_NONE`.
  - The controller's `secure_ip` exempts only the real peer, so Caddy traffic still needs the password.
  - Redis runs with `requirepass`, AOF+RDB and `noeviction`, and is unpublished.
  - Bootstrap renders secrets without putting them on argv, into `0640` files.
- **Dashboard** (subagent, spot-checked by me).
  - Authorization scoping is correct on every read route, and all SQL is parameterised.
  - Jinja autoescape is on, and `|safe` is used only on `_h()`-built HTML.
  - CSP has `script-src 'self'`, `_safe_next` is strict, and sessions have idle and absolute expiry plus credential-version revocation.
  - The list Save is atomic, and roster/`actual_name` forgery returns 404.
- **Live retention.** No repeated re-moves of the same Trained-* UIDs, from event counts (see OPUS-CR-014 above).
