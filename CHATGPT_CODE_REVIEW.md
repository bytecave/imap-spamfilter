# ChatGPT Code Review — IMAP Spam Filter

- Date: 2026-09-08
- Reviewed revision: `7be8ce819545de08940ba4276b592231a4ab9b0a` (`main`, matching `origin/main` at review time)
- Review target: the current working tree in `/opt/bytelord/projects/imap-spamfilter`

## Executive summary

The current implementation is substantially aligned with the evolved architecture: shadow mode is genuinely non-destructive for Inbox/Junk/Trash, automatic fetches are size-gated, core message identity is IMAP-coordinate based, list precedence and contradictory-learning behavior match the latest decisions, and the shared Bayes-user workflow is present.

I found **2 high-severity, 9 medium-severity, and 8 low/hardening findings**. The two release blockers are:

1. An allowlist decision made during the move grace period does not cancel an already queued spam move. A message can therefore be moved to Junk after the system has classified it as allowlisted.
2. `deploy/fix-cursor-apparmor.sh` installs a predictable, possibly attacker-supplied `/tmp` package as root without authenticating it.

I recommend keeping production mail action in `shadow` mode until **IMAP-CR-001** is fixed and tested. Do not run the AppArmor helper as root until **IMAP-CR-002** is fixed.

This review applies the stated operating assumptions: the service is private to NetBird, will not be Internet-exposed, uses a separate Microsoft 365 OAuth proxy where required, and does not need native OAuth2. I therefore do **not** report the simple dashboard credential model or lack of native OAuth2 as vulnerabilities. Unraid-specific upstream deployment material was ignored, except for code that the VPS deployment path actually invokes.

## Severity guide

- **High:** can violate the central mail-routing safety contract or yield root code execution in a documented administrative workflow.
- **Medium:** can cause lost learning, incorrect durable/operator state, crashes, materially misleading decisions, or local disclosure.
- **Low:** bounded correctness, diagnostics, documentation, or defense-in-depth issue.
- **Hardening:** an explicitly deferred risk that remains worth closing before treating the build as reproducible or supply-chain hardened.

## Findings index

| ID | Severity | Area | Finding |
|---|---:|---|---|
| IMAP-CR-001 | High | Routing safety | Allowlisting does not cancel a queued move |
| IMAP-CR-002 | High | Local supply chain | Root helper trusts an unauthenticated predictable `/tmp` package |
| IMAP-CR-003 | Medium | Learning | Message pruning silently expires Inbox-to-Junk correlation |
| IMAP-CR-004 | Medium | Configuration | Required string fields are not type-validated |
| IMAP-CR-005 | Medium | List dashboard | Entry cap comes from the first account, not YAML defaults |
| IMAP-CR-006 | Medium | Dashboard resilience | Semantic config errors escape the intended error path |
| IMAP-CR-007 | Medium | Bootstrap state | `--move-to` leaves the database at the source folder |
| IMAP-CR-008 | Medium | Identity/diagnostics | Operator surfaces still use non-unique Message-ID as a locator |
| IMAP-CR-009 | Medium | Metrics | Catch rate mixes list moves with scored-message scans |
| IMAP-CR-010 | Medium | Dashboard resilience | Rspamd JSON shape is trusted and can produce a 500 |
| IMAP-CR-011 | Medium | Local privacy | SQLite state and sidecars are world-readable on the deployed host |
| IMAP-CR-012 | Low | Fetch discipline | `explain_score.py` bypasses the 5 MiB body-fetch cap |
| IMAP-CR-013 | Low | Metrics/health | “Total” counters expire and health omits quiet/broken accounts |
| IMAP-CR-014 | Low | Bootstrap identity | Missing UIDVALIDITY is replaced with the synthetic value `1` |
| IMAP-CR-015 | Low | Dependency security | Flask is pinned to a version with a known fixed advisory |
| IMAP-CR-016 | Hardening | Reproducibility | Transitives, images, and CI actions remain mutable |
| IMAP-CR-017 | Low | Data integrity | List-table invariants are declared but not enforced in Python |
| IMAP-CR-018 | Low | Resource bounds | Stored Rspamd detail can exceed its documented cap |
| IMAP-CR-019 | Low | Documentation | README gives conflicting rate-limit/safe-mode behavior |

---

## IMAP-CR-001 — Allowlisting does not cancel a queued move

**Severity:** High

**Files:** [`filter/filter.py`](filter/filter.py#L2869), [`filter/test_address_lists.py`](filter/test_address_lists.py), [`filter/test_shadow_mode.py`](filter/test_shadow_mode.py)

### Impact

In `move` mode, a high-scoring or blocklisted Inbox message is inserted into `pending_move`. If the operator adds the sender to an allowlist during the grace period, a forced re-evaluation records `our_action="allowlisted"` but leaves the pending row intact. `execute_due_moves()` later selects that row and moves the message without re-evaluating the current list decision or even checking `our_action`.

Normal polling makes the problem more persistent: once the Inbox bookmark advances, editing a list does not itself rescan the old UID. The grace-period mechanism is supposed to preserve an opportunity to correct a decision, but the most direct correction cannot stop the queued action.

I reproduced this against the current code: queue a message at score 9, add a matching person allow entry, rescan it as allowlisted, then execute due moves. The IMAP move still occurs and the row finishes as `moved_to_junk`.

### Requirements trace

- **Requirement:** allowlisted messages stay in Inbox; later list policy says list hits bypass `/checkv2`, and an allow result wins an exact-precedence tie. See [`README.md`](README.md#L30) and the final list/learning decisions in [`23__user_list_overrides`](imap-spamfilter-plans-2026-09-08/00-chronological/23__2026-09-05_225810__user_list_overrides_8a393526.plan.md) and [`27__list-contradict_learn_skip`](imap-spamfilter-plans-2026-09-08/00-chronological/27__2026-09-06_031446__list-contradict_learn_skip_17f4c624.plan.md).
- **Architecture/acceptance:** list classification is a terminal routing decision before Rspamd; move mode delays the destructive routing action by a grace period.
- **Implementation:** the allow branch only updates the message action and logs events ([`filter.py:2869`](filter/filter.py#L2869)); the queue is created independently ([`filter.py:3011`](filter/filter.py#L3011)); due execution trusts queue membership and FLAGS, not current policy ([`filter.py:3029`](filter/filter.py#L3029)).
- **Coverage gap:** the allow-with-prior-score test does not seed a pending move. The only exercised `execute_due_moves()` behavior in the current suite is the shadow-mode early return; the normal body was uncovered in the branch-aware coverage run.

### Recommendation and required tests

Cancel the matching `pending_move` atomically whenever the effective decision becomes allow, and revalidate the effective list decision immediately before executing every due move. The execution-time check is needed to close the race between list editing and batch execution; cancellation alone is insufficient.

Add tests for:

1. scored-spam pending move → person-address allow → grace expires: pending row is deleted and no move occurs;
2. blocklist pending move → higher-precedence user allow: no move;
3. Sender-only allow match during grace: no move;
4. list changes after rows are loaded but before `client.move()`: fail closed/skip that UID;
5. removal of an allow entry does not accidentally cancel an otherwise valid, newly created pending move.

## IMAP-CR-002 — Root helper trusts an unauthenticated predictable `/tmp` package
# IGNORE THIS ENTIRE CR ISSUE. IT IS UNRELATED TO THE imap-spamfilter project. Proceed to IMAP-CR-003. [Rich, 9/8/2026]
**Severity:** High

**File:** [`deploy/fix-cursor-apparmor.sh`](deploy/fix-cursor-apparmor.sh#L6)

### Impact

The helper must run as root, chooses the fixed path `/tmp/cursor-sandbox-apparmor.deb`, skips the download if any regular file already exists there, and passes that file to `dpkg -i`. A less-privileged local user can pre-create a malicious package at that path before an administrator runs the documented helper, resulting in root code execution. Even a fresh download is not verified by a pinned checksum or signature.

The fact that the URL is HTTPS does not authenticate a pre-existing local file, and it does not give the script a reproducible artifact identity.

### Requirements trace

- **Requirement/architecture:** the operations/supply-chain slice requires fail-closed secret and artifact handling; see [`09__slice7_ops_secrets_supply_chain`](imap-spamfilter-plans-2026-09-08/00-chronological/09__2026-08-26_213225__slice7_ops_secrets_supply_chain.md).
- **Implementation:** fixed shared path at lines 6–7, root requirement at line 15, conditional download at lines 22–25, privileged install at line 28.
- **Coverage gap:** shell syntax is checked, but there is no behavioral test for unsafe pre-existing files, symlinks, wrong content, or checksum failure.

### Recommendation and required tests

Create a root-owned directory with `mktemp -d`, download to a newly created file, reject symlinks, verify an independently pinned vendor checksum/signature, install only after verification, and clean up with a trap. Do not reuse a shared cached file merely because it exists.

Add a shell integration test that pre-creates both a regular file and symlink at the historical path and proves neither can be installed. Test corrupted content, wrong checksum, download failure, and successful verified installation with `dpkg` stubbed.

## IMAP-CR-003 — Message pruning silently expires Inbox-to-Junk correlation

**Severity:** Medium

**File:** [`filter/filter.py`](filter/filter.py#L1729)

### Impact

`prune_messages()` deletes every old message row that lacks `pending_learn`; it does not retain a compact fingerprint for messages that remain in Inbox. `_run_account()` sets the horizon to `max(message_retention_days, 14) + 7`, so the default learning window is effectively 21 days (69 days in the reviewed live config with 62-day retention).

If a user moves an older Inbox message to Junk after that cutoff, `poll_junk()` can no longer find its body-SHA sibling. By design it treats the message as provider-delivered Junk and does not learn spam. This is a silent loss of the main feedback mechanism, and neither README nor the acceptance criteria disclose the age limit.

### Requirements trace

- **Requirement:** Inbox → Junk is explicit user feedback and should train spam; see [`README.md`](README.md#L30).
- **Architecture/acceptance:** slice 5 chose body SHA-256 plus IMAP identity to correlate moves, with Message-ID metadata-only; see [`07__slice5_message_identity`](imap-spamfilter-plans-2026-09-08/00-chronological/07__2026-08-26_204222__slice5_message_identity.md#L41).
- **Implementation:** all resolved rows are removed at [`filter.py:1729`](filter/filter.py#L1729), while Junk polling requires a retained sibling before it schedules learning.
- **Coverage gap:** `prune_messages()` was wholly uncovered. No test ages a still-in-Inbox row through pruning and then simulates an Inbox-to-Junk move.

### Recommendation and required tests

Choose and document an explicit feedback horizon. Prefer retaining a compact, bounded fingerprint tombstone for still-in-Inbox messages rather than retaining full operational rows forever. A tombstone needs only the account/identity, body SHA, original folder/time, and fields needed to prevent duplicate learning.

Add a time-controlled test that scans a message, advances beyond the prune horizon, prunes, and then presents the same body under a new Junk UID. Assert the chosen policy explicitly. Also test cleanup of fingerprints after a successful learn and bounded growth for mail that never moves.

## IMAP-CR-004 — Required string fields are not type-validated

**Severity:** Medium

**Files:** [`filter/filter.py`](filter/filter.py#L783), [`filter/test_connection.py`](filter/test_connection.py), [`filter/test_core_review_fixes.py`](filter/test_core_review_fixes.py)

### Impact

`load_accounts()` assigns YAML values such as `name`, `user`, `password`, `imap_host`, `actual_name`, and folder names directly to `Account`. Validation checks presence and values for several options but does not establish that required textual fields are strings. Some validation paths stringify values temporarily but retain the original object.

For example, `name: 123` is accepted, then startup fails at `", ".join(a.name for a in accounts)` with a `TypeError`. Other wrong types can surface as attribute errors, unhashable keys, or reconnect loops inside a worker rather than as a clear, fail-fast configuration error. I reproduced the numeric-name case.

### Requirements trace

- **Requirement/acceptance:** slice 4 requires strict startup validation and actionable failures for invalid account configuration; see [`06__slice4_connection_config`](imap-spamfilter-plans-2026-09-08/00-chronological/06__2026-08-26_204157__slice4_connection_config.md).
- **Implementation:** direct construction occurs in the account-loading block starting at [`filter.py:783`](filter/filter.py#L783); validation begins later but does not normalize/validate all string fields.
- **Coverage gap:** tests cover invalid booleans, numerics, unknown keys, and several value ranges, but not wrong YAML types for each required/optional string field.

### Recommendation and required tests

Validate every textual field with one shared helper: require `str`, trim where the contract permits, reject empty/whitespace-only values, and store the normalized result. Treat list roster keys and folder names the same way.

Add a parameterized matrix for integer, boolean, list, mapping, null, empty, and whitespace-only values across all textual fields. Assert a controlled `SystemExit`/configuration exception whose message names the account and field, before any worker is created.

## IMAP-CR-005 — Dashboard cap comes from the first account, not YAML defaults

**Severity:** Medium

**File:** [`filter/dashboard.py`](filter/dashboard.py#L1486)

### Impact

The list editor starts with the built-in cap, reloads all accounts, then overwrites the cap with `accs[0].max_list_entries`. Because account values already contain merged defaults and overrides, a per-account override on whichever account happens to be first changes a dashboard-wide invariant. Reordering accounts can therefore change whether the same list submission is accepted. A low first-account override can block valid edits; a high first-account override can permit more entries than the declared global default.

The post-success redirect also interpolates `scope_key` directly into the query string. A valid `actual_name` containing `&`, `?`, `#`, or `=` can corrupt the PRG destination. This is a correctness problem, not an Internet-exposure finding.

### Requirements trace

- **Requirement/acceptance:** slice 12 explicitly fixes the list cap to YAML `defaults` (or the built-in fallback), not to a per-account/minimum value; see [`20__slice12_dashboard_lists`](imap-spamfilter-plans-2026-09-08/00-chronological/20__2026-09-04_021439__slice12_dashboard_lists.md#L188).
- **Implementation:** first-account selection is at [`dashboard.py:1486`](filter/dashboard.py#L1486); raw redirect interpolation is at [`dashboard.py:1512`](filter/dashboard.py#L1512).
- **Coverage gap:** list UI tests do not vary the defaults cap independently from the first account, reorder accounts, or use reserved URL characters in `actual_name`.

### Recommendation and required tests

Parse/expose the unmerged defaults value for this global cap, or add a dedicated configuration function that returns it. Generate redirects with `url_for()`/proper query encoding.

Test defaults=1000 with first-account override=1, defaults=1 with first-account override=1000, reversed account order, and person scopes containing URL-reserved characters.

## IMAP-CR-006 — Semantic config errors escape the intended dashboard error path

**Severity:** Medium

**File:** [`filter/dashboard.py`](filter/dashboard.py#L1416)

### Impact

`_list_config()` says it returns `None` for unreadable configuration and catches `Exception`, but `load_accounts()` reports semantic configuration failures using `SystemExit`, which derives from `BaseException`. Valid YAML with an unknown key, missing required value, or invalid roster can therefore escape the error-card path and terminate the request worker. The second uncaught `load_accounts()` in the POST cap path has the same problem.

### Requirements trace

- **Requirement/acceptance:** slice 12 requires the list UI to show a controlled error when `accounts.yml` cannot be read or parsed; see [`20__slice12_dashboard_lists`](imap-spamfilter-plans-2026-09-08/00-chronological/20__2026-09-04_021439__slice12_dashboard_lists.md#L203).
- **Implementation:** catch at [`dashboard.py:1416`](filter/dashboard.py#L1416); second direct load at [`dashboard.py:1492`](filter/dashboard.py#L1492).
- **Coverage gap:** tests cover normal configuration and missing-state cases but not syntactically valid, semantically invalid YAML on GET and POST.

### Recommendation and required tests

Have library-level configuration parsing raise a dedicated `ConfigError`; translate that to `SystemExit` only in the CLI entry point. Catch `ConfigError` in the dashboard and render a non-secret, actionable 503/error card. Load configuration once per request.

Test unknown keys, missing `actual_name`, malformed roster entries, invalid modes, and wrong field types on both GET and POST. Assert that a subsequent request on the same app still succeeds.

## IMAP-CR-007 — `bootstrap_train --move-to` leaves database folder state stale

**Severity:** Medium

**Files:** [`filter/bootstrap_train.py`](filter/bootstrap_train.py#L204), [`filter/filter.py`](filter/filter.py#L3075), [`filter/test_bootstrap_train.py`](filter/test_bootstrap_train.py)

### Impact

Bootstrap records the source-folder message row, calls `client.move()`, and never updates `current_folder`. I reproduced a Train → Trained run where IMAP moved successfully but SQLite still said `current_folder='Train'`.

That stale state makes the dashboard and later forensic queries inaccurate, causes `explain_score` to attempt the source folder/UID, and weakens future correlation logic. The runtime move paths do update `current_folder`, demonstrating that it is part of the intended durable model.

### Requirements trace

- **Requirement/acceptance:** the bootstrap/retrain plans require auditable, idempotent training and optional post-training folder moves; see [`24__vps_bayes_retrain`](imap-spamfilter-plans-2026-09-08/00-chronological/24__2026-09-06_000117__vps_bayes_retrain_a8f5ef3a.plan.md).
- **Implementation:** source state is written around [`bootstrap_train.py:204`](filter/bootstrap_train.py#L204), but the move at lines 231–238 has no DB update. Runtime due moves update folder/action at [`filter.py:3075`](filter/filter.py#L3075).
- **Coverage gap:** bootstrap tests assert the IMAP move call, but not the persisted row after a successful or failed move.

### Recommendation and required tests

After a confirmed MOVE, update the source row's `current_folder` and record a distinct audit event. Because destination UID mapping may be unavailable, do not invent a destination UID; preserve the source identity as history and let normal folder polling establish the destination identity.

Test successful moves for learned, already-learned, aligned-list, and declined-list paths; move failure; DB failure after IMAP success; and a subsequent explain/correlation pass.

## IMAP-CR-008 — Operator surfaces still use non-unique Message-ID as a locator

**Severity:** Medium

**Files:** [`filter/explain_score.py`](filter/explain_score.py#L49), [`filter/dashboard.py`](filter/dashboard.py#L537), [`filter/test_explain_score.py`](filter/test_explain_score.py), [`filter/test_dashboard.py`](filter/test_dashboard.py)

### Impact

The core state model correctly treats `(account, folder, uidvalidity, uid)` as identity and Message-ID as metadata. Two operator paths regress toward the older assumption:

- `explain_score.py` accepts Message-ID, silently chooses one matching row by heuristic, and fetches its stored folder/UID. Duplicate or missing Message-IDs are ambiguous, and a moved row can point to a stale source UID.
- the dashboard event query joins a subject by `(account, message_id)` and selects the newest matching message. A repeated or sender-controlled Message-ID can relabel an older event with another message's subject.

This can produce a wrong explanation or misleading audit display precisely when an operator is investigating a high score.

### Requirements trace

- **Requirement/architecture:** slice 5 explicitly makes IMAP coordinates the identity and Message-ID metadata only; see [`07__slice5_message_identity`](imap-spamfilter-plans-2026-09-08/00-chronological/07__2026-08-26_204222__slice5_message_identity.md#L41).
- **Implementation:** explain lookup/selection at [`explain_score.py:49`](filter/explain_score.py#L49); event subject lookup at [`dashboard.py:537`](filter/dashboard.py#L537).
- **Coverage gap:** explain tests cover the UID path, not duplicate Message-IDs or moved/stale rows. Dashboard subject tests do not create Message-ID collisions.

### Recommendation and required tests

Make IMAP coordinates the canonical CLI selector. If Message-ID lookup remains as a convenience, reject ambiguity and print all candidate coordinates rather than silently choosing. For events, snapshot safe display metadata at event creation or attach object coordinates to the event schema and join by those coordinates.

Test duplicate Message-IDs across accounts/folders/UIDVALIDITY epochs, absent Message-ID, a moved source row, and an event whose Message-ID is later reused with a different subject.

## IMAP-CR-009 — Catch rate mixes list moves with scored-message scans

**Severity:** Medium

**File:** [`filter/dashboard.py`](filter/dashboard.py#L1063)

### Impact

The dashboard's numerator counts `moved%` events, while its denominator counts `scan` events. The final list policy intentionally skips `/checkv2` for list hits, so a blocklisted message can produce a move without a scan. The displayed percentage can exceed 100%, or show no denominator despite real routing activity.

### Requirements trace

- **Requirement/architecture:** final list behavior says all list hits bypass Rspamd; see [`26__list_hits_vs_training`](imap-spamfilter-plans-2026-09-08/00-chronological/26__2026-09-06_012000__list_hits_vs_training_89b59683.plan.md).
- **Implementation:** KPI queries are at [`dashboard.py:1063`](filter/dashboard.py#L1063); list-hit scan bypass is at [`filter.py:2869`](filter/filter.py#L2869).
- **Coverage gap:** no dashboard KPI test mixes scored traffic, allowlist decisions, and blocklist-forced moves.

### Recommendation and required tests

Either define the metric as “Rspamd-scored spam rate” and count only scored moves in the numerator, or define a routing catch rate whose denominator includes every terminal routing decision. Label it precisely.

Test blocklist-only traffic, allowlist-only traffic, mixed list/scored traffic, and a period with moves but no scans. Assert the percentage is mathematically bounded and the label matches its population.

## IMAP-CR-010 — Rspamd JSON shape is trusted and can produce a 500

**Severity:** Medium

**File:** [`filter/dashboard.py`](filter/dashboard.py#L705)

### Impact

`_rspamd_stats()` checks the HTTP status and then returns any decoded JSON. Summary rendering assumes a mapping with numeric `uptime`, a mapping of action counters, and mapping-like statfile rows. A 200 response containing a list, strings in numeric fields, null/malformed nested objects, or a schema change can therefore turn the dashboard into a 500 instead of showing Rspamd as unavailable/degraded.

### Requirements trace

- **Requirement/acceptance:** dashboard hardening requires external-service failures to be bounded and rendered without breaking the dashboard; see [`10__slice8_dashboard_hardening`](imap-spamfilter-plans-2026-09-08/00-chronological/10__2026-08-26_214913__slice8_dashboard_hardening.md).
- **Implementation:** trust boundary at [`dashboard.py:705`](filter/dashboard.py#L705), followed by shape assumptions in the summary block.
- **Coverage gap:** tests use a well-formed stats dictionary but do not fuzz or parameterize malformed successful responses.

### Recommendation and required tests

Validate the response into a small typed/internal schema, coerce only explicitly supported numeric representations, and treat all other shapes as unavailable while logging a bounded diagnostic.

Add a matrix for top-level list/string/null, string/null uptime, non-map actions, non-list statfiles, non-map statfile entries, huge values, and response read/JSON-decoding exceptions. Every case should return a usable page.

## IMAP-CR-011 — SQLite state and sidecars are world-readable on the deployed host

**Severity:** Medium

**Files:** [`filter/filter.py`](filter/filter.py#L1137), [`unraid/bootstrap.sh`](unraid/bootstrap.sh#L63)

### Impact

Database creation relies on process defaults and does not set a private umask, directory mode, or file mode. The deployment bootstrap creates the state directory as `0755`. On the reviewed host, `spamfilter.db`, `spamfilter.db-wal`, and `spamfilter.db-shm` were all `0644`, and the state directory was `2755`.

The database contains account identifiers, sender addresses, subjects, list entries, scores, and audit activity. It does not appear to contain message bodies or account passwords, but unrelated local Unix users can read sensitive mail metadata. Private NetBird exposure does not mitigate same-host file access.

### Requirements trace

- **Requirement/architecture:** the ops/secrets work treats mail/account material as private and expects secure host-side state; see [`09__slice7_ops_secrets_supply_chain`](imap-spamfilter-plans-2026-09-08/00-chronological/09__2026-08-26_213225__slice7_ops_secrets_supply_chain.md).
- **Implementation:** database initialization begins at [`filter.py:1137`](filter/filter.py#L1137); the deployment bootstrap creates state paths at [`unraid/bootstrap.sh:63`](unraid/bootstrap.sh#L63). This script is in scope because the VPS deployment wrapper invokes it despite its historical directory name.
- **Coverage gap:** secret-file permissions are tested, but database directory/file/sidecar permissions are not.

### Recommendation and required tests

Create the state directory as `0700`, launch state-writing processes with `umask 077`, and verify/chmod the DB plus WAL/SHM artifacts to `0600`. Ensure the container UID still has required access and document the host owner/group.

Add a deployment smoke test that creates and writes a WAL database, then asserts modes for the directory, DB, WAL, and SHM. Include upgrade handling for existing permissive files.

## IMAP-CR-012 — `explain_score.py` bypasses the 5 MiB body-fetch cap

**Severity:** Low

**Files:** [`filter/explain_score.py`](filter/explain_score.py#L37), [`filter/filter.py`](filter/filter.py), [`filter/test_explain_score.py`](filter/test_explain_score.py)

### Impact

The operator tool requests `BODY.PEEK[]` and `RFC822.SIZE` in the same fetch. The automatic filter deliberately fetches size first and never downloads an oversized body. Running explain against a very large message can therefore consume substantially more memory/network than the documented 5 MiB discipline suggests.

### Requirements trace

- **Requirement/acceptance:** slice 2 and README impose metadata-first, fail-closed maximum-fetch behavior; see [`04__slice2_imap_fetch_discipline`](imap-spamfilter-plans-2026-09-08/00-chronological/04__2026-08-26_191859__slice2_imap_fetch_discipline.md) and [`README.md`](README.md#L868).
- **Implementation:** unconditional combined fetch at [`explain_score.py:37`](filter/explain_score.py#L37); the safe reusable pattern exists in the main filter.
- **Coverage gap:** explain tests do not present an oversized message and assert that no body fetch occurs.

### Recommendation and required tests

Reuse `fetch_under_cap()` (or a shared equivalent) and report that the selected message exceeds the configured limit. Test exact-limit, one-byte-over, malformed/missing size, missing UID, and body absent after an allowed metadata fetch.

## IMAP-CR-013 — “Total” counters expire and health omits quiet/broken accounts

**Severity:** Low

**Files:** [`filter/dashboard.py`](filter/dashboard.py#L1084), [`filter/filter.py`](filter/filter.py#L1718)

### Impact

The dashboard labels ham/spam learning counts as totals, but derives them from `events`; `prune_events()` deletes events older than 30 days. These “totals” can decrease after pruning or restart history cleanup.

The global health banner is also inferred from recent/event-derived account data. A configured account with no recent events can be absent, and connection failures such as `conn_error` are not sufficient to make the banner unhealthy. The UI can say all accounts are healthy while a quiet account is disconnected. README acknowledges that the container healthcheck is process-level rather than per-worker; the dashboard label should not imply stronger knowledge than it has.

### Requirements trace

- **Requirement:** README presents total learned counts and operational visibility; see [`README.md`](README.md#L415) and [`README.md`](README.md#L745).
- **Implementation:** count queries at [`dashboard.py:1084`](filter/dashboard.py#L1084), event deletion at [`filter.py:1718`](filter/filter.py#L1718), and health/account aggregation in the dashboard summary.
- **Coverage gap:** no test advances time through event pruning, loads a configured account with zero events, or emits only connection-error events.

### Recommendation and required tests

Use durable aggregate counters or label these values “last 30 days.” Build the account roster from validated configuration, and derive per-account health from an explicit worker heartbeat/error table with a staleness threshold.

Test pruning across the 30-day boundary, a new account with no events, a stale heartbeat, persistent connection errors, and recovery. Assert that the global banner is the conjunction of every configured account's state.

## IMAP-CR-014 — Missing UIDVALIDITY is replaced with the synthetic value `1`

**Severity:** Low

**Files:** [`filter/bootstrap_train.py`](filter/bootstrap_train.py#L83), [`filter/test_bootstrap_train.py`](filter/test_bootstrap_train.py)

### Impact

Bootstrap returns `1` when SELECT omits or malforms UIDVALIDITY. This invents an IMAP epoch and can collide with a real epoch or misstate audit/idempotency rows. The main filter is stricter and treats UIDVALIDITY as required identity input.

### Requirements trace

- **Requirement/architecture:** slice 5 makes UIDVALIDITY part of the canonical message identity; see [`07__slice5_message_identity`](imap-spamfilter-plans-2026-09-08/00-chronological/07__2026-08-26_204222__slice5_message_identity.md).
- **Implementation:** fallback at [`bootstrap_train.py:83`](filter/bootstrap_train.py#L83); the main path reads the server-provided value without inventing an epoch.
- **Coverage gap:** missing/malformed UIDVALIDITY fallback branches were uncovered.

### Recommendation and required tests

Fail the folder safely before fetching, learning, moving, or writing message state if a positive integer UIDVALIDITY cannot be obtained. Test missing, null, nonnumeric, zero, negative, and byte/string valid values, asserting no Rspamd or MOVE calls on failure.

## IMAP-CR-015 — Flask is pinned to a version with a known fixed advisory

**Severity:** Low

**File:** [`filter/requirements.txt`](filter/requirements.txt#L4)

### Impact

`pip-audit` reports Flask 3.1.2 affected by `PYSEC-2026-2151` / `GHSA-68rp-wp8r-4726`, fixed in 3.1.3. The issue concerns a missing `Vary: Cookie` response header in some session-access patterns. This app's protected routes use session access and set `Cache-Control: no-store`, and it runs only on the private network, so practical exposure appears limited; the known-vulnerable pin is still unnecessary.

The authoritative [Flask advisory](https://github.com/pallets/flask/security/advisories/GHSA-68rp-wp8r-4726) rates it Low, and [Flask 3.1.3](https://github.com/pallets/flask/releases/tag/3.1.3) contains the security fix.

### Requirements trace

- **Requirement/architecture:** slice 7 calls for pinned/audited dependencies; see [`09__slice7_ops_secrets_supply_chain`](imap-spamfilter-plans-2026-09-08/00-chronological/09__2026-08-26_213225__slice7_ops_secrets_supply_chain.md).
- **Implementation:** exact Flask 3.1.2 pin at [`requirements.txt:4`](filter/requirements.txt#L4).
- **Coverage gap:** CI does not appear to run a vulnerability audit or fail on fix-available advisories.

### Recommendation and required tests

Upgrade to Flask 3.1.3 or newer compatible 3.1.x, rerun the full dashboard/auth suite, and add an automated dependency audit with a documented triage/exception policy. Include a response-header regression test for authenticated and unauthenticated/session-mutating routes.

## IMAP-CR-016 — Transitives, images, and CI actions remain mutable

**Severity:** Hardening (accepted/deferred architecture risk)

**Files:** [`filter/requirements.txt`](filter/requirements.txt), [`filter/Dockerfile`](filter/Dockerfile#L1), [`deploy/bytelord-compose.yaml`](deploy/bytelord-compose.yaml), [`.github/workflows/build.yml`](.github/workflows/build.yml)

### Impact

Direct Python requirements are pinned, but transitive dependencies are neither locked nor hash-verified. The Python base and service images use mutable tags rather than digests, the deployed filter image uses a moving tag, and GitHub Actions are referenced by version tags rather than immutable commit SHAs. Two builds from the same Git revision can therefore resolve different code.

This is not presented as an undisclosed implementation regression: slice 7 and the later status documents explicitly deferred action-SHA and moving-image work. It remains an open supply-chain/reproducibility risk, and the Flask audit result demonstrates the value of continuously rebuilding and auditing the resolved graph.

### Requirements trace

- **Architecture/status:** [`09__slice7_ops_secrets_supply_chain`](imap-spamfilter-plans-2026-09-08/00-chronological/09__2026-08-26_213225__slice7_ops_secrets_supply_chain.md) records these deferrals; [`22__IMPLEMENTATION_STATUS`](imap-spamfilter-plans-2026-09-08/00-chronological/22__2026-09-04_041029__IMPLEMENTATION_STATUS.md) continues to identify them.
- **Implementation:** mutable references in the files listed above.
- **Coverage gap:** CI validates the built result but does not prove dependency resolution is repeatable or that artifacts match an approved digest/SBOM.

### Recommendation and required tests

Generate a hash-locked Python dependency file, pin base/runtime images by digest while retaining readable version comments, pin actions by commit SHA, emit an SBOM/provenance, and use Dependabot/Renovate to refresh pins. Add CI that performs a clean locked install, vulnerability scan, and multi-architecture image smoke test.

## IMAP-CR-017 — List-table invariants are declared but not enforced in Python

**Severity:** Low

**File:** [`filter/filter.py`](filter/filter.py#L425)

### Impact

The code declares valid sets for list scope, kind, pattern type, and source, but the low-level `Db` list methods accept arbitrary strings for most of these dimensions. Current dashboard/IMAP callers constrain their values, so this is not presently an external injection path; it is an internal integrity hazard. A future caller, migration, or maintenance script can create rows the classifier/UI does not understand.

### Requirements trace

- **Requirement/acceptance:** slice 10 explicitly assigns enum checks to Python; see [`18__slice10_list_core`](imap-spamfilter-plans-2026-09-08/00-chronological/18__2026-09-04_021439__slice10_list_core.md#L186).
- **Implementation:** valid sets at [`filter.py:425`](filter/filter.py#L425), with write methods later in `Db` not consistently applying them.
- **Coverage gap:** parser tests reject malformed textual entries, but direct DB API tests do not attempt invalid scope/kind/pattern/source values.

### Recommendation and required tests

Centralize validation at the DB write boundary and retain database CHECK constraints where migrations permit. Add negative tests for every invalid enum and confirm the whole transaction rolls back. Preserve the later, intentional rule that a person-scoped pattern may be either an exact address or `@host` domain pattern.

## IMAP-CR-018 — Stored Rspamd detail can exceed its documented cap

**Severity:** Low

**File:** [`filter/filter.py`](filter/filter.py#L257)

### Impact

`score_detail_json()` progressively drops symbol descriptions/symbols to reduce large results, but it retains the Rspamd `action` field without an independent bound. A malformed or unexpectedly large action string can therefore still produce a much larger SQLite value than the intended 2–4 KiB cap, growing state and dashboard processing cost.

Rspamd is a local trusted service in this deployment, so this is defense in depth rather than an Internet-originated denial of service.

### Requirements trace

- **Requirement/acceptance:** the high-score explanation plan requires bounded stored detail; see [`25__explain_high_scores`](imap-spamfilter-plans-2026-09-08/00-chronological/25__2026-09-06_003813__explain_high_scores_f295a03f.plan.md).
- **Implementation:** size-reduction logic at [`filter.py:257`](filter/filter.py#L257) does not guarantee a final maximum for every field.
- **Coverage gap:** tests cover many/large symbols but not a huge action or a hard final serialized-size invariant.

### Recommendation and required tests

Bound every string before serialization and enforce a final byte-length check with a minimal fallback object. Test huge action, huge Unicode action, huge symbol names/descriptions, and assert the UTF-8 serialized result never exceeds the documented maximum and always remains valid JSON.

## IMAP-CR-019 — README gives conflicting rate-limit/safe-mode behavior

**Severity:** Low

**File:** [`README.md`](README.md#L532)

### Impact

The configuration table says rate-limit breaches trigger safe mode, while the later operational section describes rate limits as soft refusals and says sticky safe mode is entered only for unseen IMAP flags. The implementation follows the latter model. The README also describes a per-run training option as “actions per hour” in one place. An operator responding to a stopped move/train path can therefore diagnose the wrong state or wait for a manual safe-mode reset that was never set.

### Requirements trace

- **Architecture/acceptance:** the evolved plans distinguish soft rate refusal from sticky invariant-triggered safe mode; the later text in [`README.md`](README.md#L787) reflects this.
- **Implementation:** `check_rate()` returns refusal without setting safe mode; flag-contract failures set sticky safe mode.
- **Coverage gap:** behavior tests exist, but there is no docs/config-contract check keeping option names, units, and safety semantics synchronized.

### Recommendation and required tests

Reconcile the earlier configuration table with the later operational section and describe every limit with its actual window (`per run` versus rolling hour). Add a lightweight documentation test that extracts documented config keys/defaults/units and compares them with `BUILTIN_DEFAULTS` plus a small explicit semantics table.

---

## Verification performed

- Read `README.md` and every file in `imap-spamfilter-plans-2026-09-08/00-chronological/` in filename/creation-time order (27 documents; 7,414 lines). Later documents were treated as authoritative where decisions changed.
- Reviewed all production Python, JavaScript, Docker/Compose/CI YAML, Rspamd/Redis configuration, deployment scripts, tests, and the VPS-invoked bootstrap path.
- Confirmed local `main` and `origin/main` both resolved to `7be8ce819545de08940ba4276b592231a4ab9b0a` at review time.
- Ran the test suite in the project container: **205 passed in 26.73 seconds**.
- Ran branch-aware coverage: **71% total** (`filter.py` 68%, `dashboard.py` 75%, `bootstrap_train.py` 84%, `explain_score.py` 64%). Passing tests do not cover the normal due-move executor, message pruning, or the risky explain lookup paths called out above.
- Parsed both Compose configurations, compiled the Python sources, and ran `bash -n` over deployment/bootstrap shell scripts; all completed successfully.
- Ran `pip-audit 2.9.0 -r filter/requirements.txt`; it reported the Flask advisory in **IMAP-CR-015**.
- Built targeted reproductions for **IMAP-CR-001**, **IMAP-CR-004**, **IMAP-CR-007**, and **IMAP-CR-008** rather than relying only on static inspection.

## Controls that match the final architecture

The following important controls were inspected and found materially consistent with the latest requirements:

- Shadow mode does not automatically mutate Inbox/Junk/Trash; list folder draining and Train-folder processing remain explicitly permitted shadow writes.
- Core message identity uses account/folder/UIDVALIDITY/UID, and body SHA is used for cross-folder move correlation; Message-ID is metadata in core routing/learning.
- Inbox bookmark advancement and UIDVALIDITY handling fail closed in the main polling path.
- Automatic message retrieval applies the 5 MiB metadata-first cap.
- List matching uses From plus Sender, not Reply-To; exact person addresses and person `@host` patterns are supported; user scope outranks roster-domain scope; allow wins only an exact-precedence tie.
- List hits bypass `/checkv2`; only contradictory feedback suppresses learning, while aligned feedback still learns.
- Shared live Bayes identity and the `bootstrap --all-trained` / explanation workflows are implemented.
- Secret-file checks, log redaction, dashboard no-store headers, CSRF, and loopback/private-network deployment boundaries are present.
- IMAP destructive actions use MOVE and do not issue message deletes/expunges.

## Scope notes

- Existing working-tree changes were preserved. Before this report was added, the tree already contained a deleted tracked `CURSOR_CODE_REVIEW.md` and untracked plan/archive files.
- This review did not treat private-network-only dashboard authentication as an Internet-facing auth design, per the stated deployment boundary.
- Native OAuth2 support was not required; the separate Microsoft 365 OAuth proxy is the accepted design.
- Historical Unraid instructions were excluded except where the current VPS deployment wrapper reuses code from the `unraid/` directory.

## Code Review Disposition

### IMAP-CR-001 — Allowlisting does not cancel a queued move
- **Disposition:** Fixed
- **Reasoning:** An allow decision now drops the matching `pending_move` row in the same transaction as `our_action="allowlisted"`. `execute_due_moves()` re-fetches the body under the 5 MiB cap and re-runs `classify_list_hit()` immediately before MOVE, so a list edit after the due-row load cannot still junk the message.
- **Files changed:** `filter/filter.py`
- **Tests added or updated:** `filter/test_code_review_findings.py` (`test_allow_cancels_scored_pending_move`, `test_user_allow_beats_block_pending_move`, `test_sender_only_allow_cancels_due_move`, `test_allow_added_after_due_rows_loaded_skips_move`, `test_removing_allow_does_not_cancel_valid_pending_move`)
- **Limitations / follow-up:** Oversized due messages are left pending and skipped for that cycle (fail closed) rather than moved without a list re-check.

### IMAP-CR-002 — Root helper trusts an unauthenticated predictable `/tmp` package
- **Disposition:** No Change Necessary
- **Reasoning:** Operator annotation on 2026-09-08: ignore this finding entirely; it is unrelated to the imap-spamfilter project. `deploy/fix-cursor-apparmor.sh` was not modified.
- **Files changed:** none
- **Tests added or updated:** none
- **Limitations / follow-up:** none for this repository.

### IMAP-CR-003 — Message pruning silently expires Inbox-to-Junk correlation
- **Disposition:** Fixed
- **Reasoning:** `prune_messages()` copies still-in-Inbox body-SHA rows into `message_fingerprints` before deleting operational rows. `poll_junk()` consults those fingerprints when no live sibling remains. Successful learns drop the fingerprint. Age (400 days) and per-account cap (20 000) bound growth. README documents the horizon.
- **Files changed:** `filter/filter.py`, `README.md`
- **Tests added or updated:** `filter/test_code_review_findings.py` (`test_prune_keeps_inbox_fingerprint_for_later_junk_learn`, `test_fingerprint_growth_is_capped`)
- **Limitations / follow-up:** Fingerprints store folder/UID of the pruned Inbox object, not a new destination UID. Extremely large never-moved Inboxes rely on the 20 000 LRU-style cap.

### IMAP-CR-004 — Required string fields are not type-validated
- **Disposition:** Fixed
- **Reasoning:** YAML account and roster text fields now go through `_require_text()` (`str`, trim where allowed, reject empty/whitespace/CRLF). `load_accounts()` raises `ConfigError` instead of `SystemExit`. `tls_mode` must be a string.
- **Files changed:** `filter/filter.py`
- **Tests added or updated:** `filter/test_code_review_findings.py` (`test_required_strings_reject_wrong_yaml_types`); existing load-account tests now expect `ConfigError`
- **Limitations / follow-up:** Password is not trimmed (intentional); whitespace-only passwords still fail.

### IMAP-CR-005 — Dashboard cap comes from the first account, not YAML defaults
- **Disposition:** Fixed
- **Reasoning:** `yaml_max_list_entries()` reads unmerged YAML `defaults.max_list_entries` (else the built-in 1000). List POST uses that cap once per request. Success redirects use `url_for()`.
- **Files changed:** `filter/filter.py`, `filter/dashboard.py`
- **Tests added or updated:** `filter/test_dashboard.py` (`test_list_cap_uses_yaml_defaults_not_first_account`, `test_list_redirect_encodes_reserved_actual_name`)
- **Limitations / follow-up:** Per-account `max_list_entries` still applies to IMAP folder drains, which is the intended per-mailbox cap.

### IMAP-CR-006 — Semantic config errors escape the intended dashboard error path
- **Disposition:** Fixed
- **Reasoning:** Config parsing raises `ConfigError` (`Exception`). Dashboard `_list_config()` catches it and returns a 503 error card. CLI entry points (`filter.main`, `bootstrap_train.main`, `explain_score.main`) map `ConfigError` to a process exit. Configuration is loaded once per list request.
- **Files changed:** `filter/filter.py`, `filter/dashboard.py`, `filter/bootstrap_train.py`, `filter/explain_score.py`
- **Tests added or updated:** `filter/test_dashboard.py` (`test_invalid_accounts_yml_is_error_card_not_500`); load-account tests updated to `ConfigError`
- **Limitations / follow-up:** The error card does not echo the YAML path contents (no secret leak).

### IMAP-CR-007 — `bootstrap_train --move-to` leaves database folder state stale
- **Disposition:** Fixed
- **Reasoning:** After a confirmed IMAP MOVE, bootstrap updates the source row's `current_folder` and logs `bootstrap_moved`. Destination UIDs are not invented.
- **Files changed:** `filter/bootstrap_train.py`
- **Tests added or updated:** `filter/test_bootstrap_train.py` (`test_move_to_updates_current_folder`, `test_move_failure_leaves_source_folder`)
- **Limitations / follow-up:** A DB write failure after IMAP success is still possible (same class of gap as other post-MOVE updates); the source identity is preserved.

### IMAP-CR-008 — Operator surfaces still use non-unique Message-ID as a locator
- **Disposition:** Fixed
- **Reasoning:** `explain_score --message-id` refuses 0 or >1 matches and prints every candidate IMAP coordinate. Events now snapshot `subject` at insert time; the dashboard join uses that snapshot, falling back to a Message-ID lookup only when the match is unique.
- **Files changed:** `filter/explain_score.py`, `filter/filter.py`, `filter/dashboard.py`
- **Tests added or updated:** `filter/test_explain_score.py` (`test_explain_ambiguous_message_id_prints_candidates`); `filter/test_dashboard.py` (`test_event_subject_not_stolen_by_reused_message_id`)
- **Limitations / follow-up:** Historical event rows without `subject` still use the unique-Message-ID fallback.

### IMAP-CR-009 — Catch rate mixes list moves with scored-message scans
- **Disposition:** Fixed
- **Reasoning:** The summary KPI is now moves / (scans + allowlisted + blocklisted) in 24h, clamped to 0–100%, labeled “of routing decisions”.
- **Files changed:** `filter/dashboard.py`, `README.md`
- **Tests added or updated:** `filter/test_dashboard.py` (`test_catch_rate_bounded_with_list_moves`)
- **Limitations / follow-up:** The metric is a routing catch rate, not a pure Rspamd spam rate.

### IMAP-CR-010 — Rspamd JSON shape is trusted and can produce a 500
- **Disposition:** Fixed
- **Reasoning:** `_normalize_rspamd_stats()` accepts only a mapping with coerced numeric fields, mapping actions, and list-of-mapping statfiles. Anything else is treated as unavailable.
- **Files changed:** `filter/dashboard.py`
- **Tests added or updated:** `filter/test_dashboard.py` (`test_malformed_rspamd_stats_do_not_500`)
- **Limitations / follow-up:** Unexpected but well-typed extra keys are ignored, not rejected.

### IMAP-CR-011 — SQLite state and sidecars are world-readable on the deployed host
- **Disposition:** Fixed
- **Reasoning:** `init_db()` / `Db` apply umask 077, `STATE_DIR` mode 0700, and chmod 0600 on the DB + WAL/SHM. `unraid/bootstrap.sh` creates `state/` as 0700. `main()` also sets umask 077.
- **Files changed:** `filter/filter.py`, `unraid/bootstrap.sh`, `README.md`
- **Tests added or updated:** `filter/test_code_review_findings.py` (`test_init_db_sets_private_modes`)
- **Limitations / follow-up:** Already-running containers keep existing file modes until restart/re-init; operators should restart the filter after deploy. Host ACL/NFS mappings can still override Unix modes.

### IMAP-CR-012 — `explain_score.py` bypasses the 5 MiB body-fetch cap
- **Disposition:** Fixed
- **Reasoning:** Explain now uses `fetch_under_cap()` and reports when the selected UID exceeds the cap.
- **Files changed:** `filter/explain_score.py`
- **Tests added or updated:** `filter/test_explain_score.py` (`test_explain_skips_oversize_without_body_fetch`, `test_explain_exact_limit_fetches_body`, `test_explain_missing_size_is_fail_closed`)
- **Limitations / follow-up:** none.

### IMAP-CR-013 — “Total” counters expire and health omits quiet/broken accounts
- **Disposition:** Fixed
- **Reasoning:** Learn KPIs are labeled and queried as last 30 days. `account_heartbeat` records last_ok / last_error from the account loop. Summary health is the conjunction of configured accounts (stale/missing heartbeat, conn_error, scan/learn fails, safe-mode). The Accounts page includes the YAML roster.
- **Files changed:** `filter/filter.py`, `filter/dashboard.py`, `README.md`
- **Tests added or updated:** `filter/test_dashboard.py` (`test_catch_rate_bounded_with_list_moves` asserts 30d labels); heartbeat write covered via `test_init_db_sets_private_modes`
- **Limitations / follow-up:** Stale threshold is a fixed 20 minutes, not `2 * idle_timeout` per account. Existing deployments populate heartbeat only after the upgraded worker runs.

### IMAP-CR-014 — Missing UIDVALIDITY is replaced with the synthetic value `1`
- **Disposition:** Fixed
- **Reasoning:** Bootstrap fails the folder before fetch/learn/move/DB writes unless SELECT yields a positive integer UIDVALIDITY (bytes/str accepted).
- **Files changed:** `filter/bootstrap_train.py`
- **Tests added or updated:** `filter/test_bootstrap_train.py` (`test_uidvalidity_fail_closed_skips_learn_and_move`, `test_uidvalidity_accepts_positive_intish`)
- **Limitations / follow-up:** none.

### IMAP-CR-015 — Flask is pinned to a version with a known fixed advisory
- **Disposition:** Fixed
- **Reasoning:** Flask pin is 3.1.3. CI lint job runs `pip-audit`. Dashboard sends `Vary: Cookie` on every response.
- **Files changed:** `filter/requirements.txt`, `filter/dashboard.py`, `.github/workflows/build.yml`
- **Tests added or updated:** `filter/test_code_review_findings.py` (`test_flask_pin_is_patched`); `filter/test_dashboard.py` (`test_authenticated_responses_send_vary_cookie`)
- **Limitations / follow-up:** `pip-audit` has no documented ignore list yet because the Flask 3.1.3 graph was clean at validation time.

### IMAP-CR-016 — Transitives, images, and CI actions remain mutable
- **Disposition:** Accepted Risk
- **Reasoning:** Slice 7 and IMPLEMENTATION_STATUS explicitly deferred hash-locked transitives, image digests, and action SHAs. Closing that is a supply-chain project of its own, not a correctness regression in this slice. Direct requirements remain pinned; Flask was upgraded and audited (CR-015).
- **Files changed:** none for this finding (pip-audit in CR-015 only)
- **Tests added or updated:** none
- **Limitations / follow-up:** Still open: `requirements` lock+hashes, digest-pinned base/runtime images, GitHub Actions pinned by commit SHA, SBOM/provenance.

### IMAP-CR-017 — List-table invariants are declared but not enforced in Python
- **Disposition:** Fixed
- **Reasoning:** `Db` list writes (`list_upsert_address`, `list_flip_address`, `list_replace`) validate scope/kind/source/pattern_type against the declared enums. Invalid writes raise `ValueError` and roll back with the surrounding transaction. Person-scoped `@host` patterns remain allowed.
- **Files changed:** `filter/filter.py`
- **Tests added or updated:** `filter/test_code_review_findings.py` (`test_list_write_rejects_invalid_enums_and_rolls_back`)
- **Limitations / follow-up:** Existing DBs are not CHECK-constrained; invalid historical rows would still be readable.

### IMAP-CR-018 — Stored Rspamd detail can exceed its documented cap
- **Disposition:** Fixed
- **Reasoning:** `score_detail_json()` clips `action`/names/descriptions before serialize, then trims symbols, then falls back to `{score, action: null, symbols: []}` so UTF-8 length never exceeds `SCORE_DETAIL_MAX_BYTES`.
- **Files changed:** `filter/filter.py`
- **Tests added or updated:** `filter/test_code_review_findings.py` (`test_score_detail_json_caps_huge_action_and_unicode`)
- **Limitations / follow-up:** none.

### IMAP-CR-019 — README gives conflicting rate-limit/safe-mode behavior
- **Disposition:** Fixed
- **Reasoning:** The configuration table now matches the operational section: rolling-hour soft refusal for moves/learns, per-run cap for train drains, sticky safe-mode only for UNSEEN-over-cap. A contract test compares documented keys/defaults with `BUILTIN_DEFAULTS`.
- **Files changed:** `README.md`
- **Tests added or updated:** `filter/test_code_review_findings.py` (`test_readme_rate_limit_contract_matches_builtins`)
- **Limitations / follow-up:** The contract test checks key presence and defaults, not every prose sentence.

### Validation commands and results

```text
docker run --rm -v /opt/bytelord/projects/imap-spamfilter:/src -w /src/filter \
  python:3.12-slim bash -c \
  "pip install -q -r requirements.txt pytest==8.4.2 && python -m pytest -q --tb=short"
```

**Result:** `286 passed in 39.59s`

```text
docker run --rm -v /opt/bytelord/projects/imap-spamfilter:/src -w /src \
  python:3.12-slim bash -c \
  "pip install -q -r filter/requirements.txt pip-audit==2.9.0 && pip-audit -r filter/requirements.txt"
```

**Result:** `No known vulnerabilities found` (exit 0)

```text
bash -n deploy/*.sh unraid/*.sh
```

**Result:** exit 0 (no syntax errors)

Unrelated working-tree paths were left untouched: `.gitattributes`, `deploy/fix-cursor-apparmor.txt`, `imap-spamfilter-plans-2026-09-08/`.
