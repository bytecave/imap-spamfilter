# CURSOR_CODE_REVIEW

Review date: 2026-08-28

Scope: current `main` source, tests, runtime configuration, container/deployment
definitions, bootstrap scripts, CI, and the Slice 1-8 architecture documents.
This was a read-only first pass except for this report.

Validation performed:

- The architecture graph was queried before source inspection.
- All 74 existing Python tests pass under the documented `uv` test command.
- Both shell scripts pass `bash -n`.
- Both Compose files pass `docker compose config --quiet`.
- A repository secret scan found no committed private credential. The reported
  `fuzzy_check.conf` key is explicitly the published public key for
  `fuzzy.rspamd.com`; ignored `graphify-out` hashes are not credentials.

Passing tests do not negate the findings below: most are replay, race, malformed
configuration, deployment-contract, and failure-injection cases that the current
fakes do not exercise.

## High-severity findings

1. **CR-001 [HIGH][DATABASE] Legacy database startup fails before migration**
   - Locations: `filter/filter.py:616-640`, `filter/filter.py:699-718`,
     `filter/filter.py:769-778`; test gap: `filter/test_message_identity.py:126-184`.
   - `init_db()` executes the current schema, including an index on
     `messages.body_sha256`, before `_migrate()` adds that column to a legacy
     table. SQLite raises `no such column: body_sha256`, so the migration is
     unreachable during real startup. The migration test invokes `_migrate()`
     directly and therefore misses the actual ordering failure.
   - Create/migrate tables before creating new-column indexes and test
     `init_db()` against every supported legacy schema.

2. **CR-002 [HIGH][IMAP][CORRECTNESS] `bookmark + 1:*` can include an old UID**
   - Locations: `filter/filter.py:1707-1713`, `filter/filter.py:1739-1886`;
     test gap: `filter/test_inbox_bookmark.py:31-123`.
   - IMAP ranges are order-independent, and RFC 3501 explicitly says a range
     such as `559:*` always includes the mailbox's highest UID even when 559 is
     greater than every assigned UID. When no new mail exists,
     `bookmark+1:*` can therefore return the bookmark UID. Unlike `poll_junk()`
     at line 1973, Inbox results are not filtered with `uid > bookmark`.
   - This can rescan or even act on the highest pre-existing message that the
     initialization watermark was intended to protect. Filter both search
     results to UIDs strictly greater than the bookmark, or use UIDNEXT.

3. **CR-003 [HIGH][IMAP][RACE] Mail arriving between the two Inbox searches is skipped**
   - Locations: `filter/filter.py:1710-1713`, `filter/filter.py:1815-1818`,
     `filter/filter.py:1884-1886`.
   - A new unread message can arrive after the `UNSEEN` search but before the
     all-UID search. It appears only in `new_uids`, is classified as already
     seen by `uid not in unseen`, and then advances the bookmark. It is never
     scored.
   - Snapshot candidate UIDs first and constrain the unread query to that
     snapshot, or determine seen state from the FLAGS already fetched with each
     candidate. Add a fake that injects an arrival between searches.

4. **CR-004 [HIGH][REPLAY] Persisted scores suppress interrupted actions**
   - Locations: `filter/filter.py:1811-1814`, `filter/filter.py:1845-1882`.
   - The score commits before flagging or durable move intent. If `add_flags()`,
     the pending-move transaction, or the process fails afterward, replay sees
     `our_score IS NOT NULL` and treats the message as complete. The requested
     action is never retried.
   - Persist score and durable action intent atomically. A scored row should be
     terminal only when the action required by the active mode is complete.
     Add failure-injection tests after the score commit.

5. **CR-005 [HIGH][IDEMPOTENCY][BAYES] An uncertain filter MOVE can be learned as user spam**
   - Locations: `filter/filter.py:1904-1945`, `filter/filter.py:2004-2029`.
   - If the server completes `MOVE` but the response is lost, or the DB update
     fails afterward, the source row remains `pending_move`. On reconnect the
     missing source UID is dropped; Junk polling sees an Inbox sibling whose
     action is not `moved_to_junk` and interprets the filter's own move as
     explicit user feedback.
   - Treat both `pending_move` and `moved_to_junk` as filter-owned provenance,
     persist an operation state before mutating IMAP, and reconcile uncertain
     outcomes (prefer MOVEUID/UIDPLUS where available).

6. **CR-006 [HIGH][CONFIG][FAIL-CLOSED] NaN values bypass score safeguards**
   - Locations: `filter/filter.py:498-500`, `filter/filter.py:565-580`,
     `filter/filter.py:1140-1148`, `filter/filter.py:1850-1881`.
   - YAML accepts `.nan`. Comparisons with NaN are false, so
     `threshold: .nan` passes validation and every finite score reaches the
     over-threshold action path. A non-finite score from Rspamd also passes the
     range check. Non-object JSON can additionally raise an uncaught
     `AttributeError`, and booleans are accepted as numbers.
   - Require real, non-boolean, finite numbers (`math.isfinite`) in
     configuration and Rspamd responses; validate that response JSON is a
     mapping and catch conversion errors.

7. **CR-007 [HIGH][DEPLOYMENT][SECRETS] The recommended Unraid install cannot start**
   - Locations: `unraid/bootstrap.sh:10-15`, `unraid/bootstrap.sh:168-191`,
     `unraid/spamfilter.xml:35-42`, `filter/filter.py:81-101`,
     `filter/filter.py:2544-2549`, `README.md:128-200`, `README.md:226-234`.
   - Default Unraid bootstrap requires ByteLord's VPS-only
     `/opt/bytelord/secrets/imap-spamfilter.env` and aborts when it is absent.
     Bootstrap deletes the old state password files, while the Unraid filter
     template neither mounts the replacement secret nor supplies
     `RSPAMD_PASSWORD`; the daemon exits with status 2. The XML and README still
     promise a `state/controller.password` fallback.
   - Define one tested Unraid secret contract: mount a protected secrets file
     and set `SECRETS_FILE`, or restore the protected state-file handoff.
     Update bootstrap, XML, README, backup instructions, and tests together.

8. **CR-008 [HIGH][SECURITY][SECRETS] Rendered controller and Redis passwords are world-readable**
   - Locations: `unraid/bootstrap.sh:57-64`,
     `unraid/bootstrap.sh:193-231`; requirement:
     `design-arch/slice7_ops_secrets_supply_chain.md:58-64`.
   - Passwords from a mode-600 source are rendered into mode-644 files under
     traversable directories. Any local host user can recover the privileged
     Rspamd controller and Redis credentials. This directly regresses Slice 7,
     which requires mapped ownership and mode 0640.
   - Use 0600/0640 with verified container UID/group ownership or ACLs. Fail
     bootstrap rather than falling back to world-readable secrets.

9. **CR-009 [HIGH][CI][SUPPLY-CHAIN] Images publish before tests succeed**
   - Location: `.github/workflows/build.yml:27-69`,
     `.github/workflows/build.yml:71-98`.
   - The image build/push and test jobs are independent. A main or tag run can
     publish `latest`, SHA, and release tags while tests fail concurrently.
   - Gate publication on the lint/test job, or separate an unpushed build from
     a test-dependent publish job.

10. **CR-010 [HIGH][ERROR-CLASSIFICATION][DATA-QUALITY] Infrastructure failures discard explicit training**
    - Locations: `filter/filter.py:1151-1201`,
      `filter/filter.py:2128-2151`, `filter/filter.py:2212-2235`;
      tests: `filter/test_learn.py:149-166`, `filter/test_learn.py:260-279`.
    - Authentication failures, 429s, 5xx responses, and network failures all
      collapse to `error`. After three attempts, pending and Train-* workflows
      mark the message `unlearnable`; the Train-* path then moves it out
      untrained. A temporary Rspamd outage or wrong controller password can
      permanently discard many explicit user corrections.
    - Distinguish content-terminal decline from authentication/configuration,
      throttling, and transient service failures. Preserve explicit training
      until infrastructure recovers; use backoff/safe-mode and alert on
      auth/config errors rather than converting them to message state.

## Medium-severity findings

11. **CR-011 [MEDIUM][DATABASE][MIGRATION] The legacy rebuild is not atomic or restart-safe**
    - Location: `filter/filter.py:718-767`.
    - The migration creates `messages_imap`, copies rows, drops the source, and
      renames through separate statements/`executescript()` boundaries. A crash
      can leave `messages_imap` present; the next run's unconditional
      `CREATE TABLE messages_imap` then fails. There is no migration version or
      recovery path.
    - Run the rebuild in one explicit transaction with a version marker and
      make each intermediate state recoverable/idempotent. Do not repurpose
      `PRAGMA user_version` as the vacuum timestamp (`filter/filter.py:1056-1069`).

12. **CR-012 [MEDIUM][RETRY] Failed immediate keyword learns are not queued**
    - Locations: `filter/filter.py:1791-1809`,
      `filter/filter.py:2023-2044`.
    - `$NotJunk`/`$Junk` bypasses the grace period, but the return from
      `try_learn()` is ignored. On a rate limit, safe-mode, or Rspamd failure,
      Inbox marks the UID terminal and Junk advances its bookmark without
      recording a pending retry.
    - Queue a pending learn for retryable outcomes and distinguish policy
      refusal from service failure in the return contract.

13. **CR-013 [MEDIUM][UIDVALIDITY] Pending learns can fetch from the wrong UID epoch**
    - Locations: `filter/filter.py:2049-2126`; contrast
      `filter/filter.py:1300-1342`.
    - `process_pending_learns()` selects each folder directly and never compares
      the selected UIDVALIDITY with the row's stored value. If UIDVALIDITY
      changes between normal checks and this fetch, the old UID can identify
      unrelated mail and train its bytes.
    - Select through the UIDVALIDITY guard, compare every candidate's epoch,
      and cancel stale candidates before fetching.

14. **CR-014 [MEDIUM][UIDVALIDITY][DATABASE] Pending-move cleanup is not folder-scoped**
    - Locations: `filter/filter.py:649-655`,
      `filter/filter.py:1300-1313`.
    - UIDVALIDITY is scoped to a mailbox, but `pending_move` stores no folder
      and cleanup deletes by account plus numeric UIDVALIDITY. A Train-* or Junk
      UIDVALIDITY change can delete unrelated Inbox move intents that happen to
      have the same numeric value.
    - Add folder to the pending-move key/queries, or restrict cleanup to the
      Inbox object the table actually represents.

15. **CR-015 [MEDIUM][IDENTITY] Retry accounting still uses Message-ID**
    - Locations: `filter/filter.py:2133-2149`,
      `filter/filter.py:2216-2235`; requirement:
      `design-arch/slice5_message_identity.md:19-27`.
    - Duplicate sender-controlled Message-IDs share failure history, so one
      object can make another give up early. Pending learns with NULL
      Message-ID never match `message_id=?`; Train-* rows with NULL use
      `message_id IS ?` and share one account-wide bucket.
    - Store attempt state on the IMAP-object row keyed by
      `(account, folder, uidvalidity, uid, kind)`. Add duplicate and NULL
      Message-ID tests.

16. **CR-016 [MEDIUM][IDENTITY][BAYES] Exact-body hashes are treated as proof of a MOVE**
    - Locations: `filter/filter.py:827-834`,
      `filter/filter.py:1393-1405`, `filter/filter.py:1771-1809`,
      `filter/filter.py:1994-2029`.
    - A byte-identical redelivery, copy, or stale row from an old UID epoch can
      be interpreted as an Inbox/Junk transition. This can train provider Junk
      as user spam or treat a fresh Inbox delivery as a ham revert.
    - Require an unambiguous, recent source-to-destination transition, source
      disappearance, and compatible UID epochs. Fail closed when multiple
      sibling candidates exist; consume correlation intent once.

17. **CR-017 [MEDIUM][RETRY][IMAP] Junk watermark skips transient missing bodies**
    - Locations: `filter/filter.py:1972-1989`,
      `filter/filter.py:2043-2044`; test gap:
      `filter/test_fetch_discipline.py:169-189`.
    - If metadata succeeds but the below-cap body fetch omits one UID,
      processing continues and the watermark advances to `max(uids)`. That
      explicit user move is never reconsidered.
    - Use terminal-prefix advancement as Inbox does, or persist failed UIDs for
      retry.

18. **CR-018 [MEDIUM][CONFIG][PRECEDENCE] Per-account `ssl` cannot override default `tls_mode`**
    - Locations: `filter/filter.py:343-374`; requirement:
      `README.md:378-381`; test gap: `filter/test_connection.py:31-34`.
    - With `defaults.tls_mode: implicit` and account-level `ssl: false`, the
      merged resolver sees both keys and always chooses `tls_mode`, violating
      per-account precedence. Port-143 proxy accounts then attempt implicit TLS.
    - Resolve `tls_mode`/deprecated `ssl` within the account layer first, then
      fall back to defaults. Add cross-layer conflict tests.

19. **CR-019 [MEDIUM][CONFIG][VALIDATION] Unknown keys are silently ignored**
    - Locations: `filter/filter.py:448-525`; examples:
      `accounts.yml.example:83-92`.
    - A typo such as `junk_retention_day: 0` is accepted but unused, leaving the
      ten-day default active when the account leaves shadow mode. This can move
      mail earlier than the operator intended.
    - Reject unknown root/default/account keys using an explicit schema and
      suggest close matches.

20. **CR-020 [MEDIUM][CONFIG][LIFECYCLE] Invalid timing values can create tight loops**
    - Locations: `filter/filter.py:503-506`,
      `filter/filter.py:606-609`, `filter/filter.py:404-445`,
      `filter/filter.py:2453-2459`.
    - Zero/negative `idle_timeout` causes rapid IDLE enter/exit and repeated
      scans; zero/negative Junk or retention intervals run those paths every
      cycle. Current validation checks grace/retention days but not loop timing.
    - Define positive lower/upper bounds for every interval and test boundary
      values.

21. **CR-021 [MEDIUM][TOOLING][RESOURCE] Bulk training performs an unbounded full-body fetch**
    - Locations: `filter/bootstrap_train.py:41`, `filter/bootstrap_train.py:67-78`.
    - One request fetches up to 10,000 complete messages with no size cap or
      chunking. A realistic mailbox can consume gigabytes, exceed connection
      timeouts, or OOM the container.
    - Reuse `fetch_under_cap()` in bounded chunks, checkpoint progress, and
      apply retry/backoff.

22. **CR-022 [MEDIUM][TOOLING][CONTRACT] Partial bootstrap training exits successfully**
    - Locations: `filter/bootstrap_train.py:79-109`.
    - Missing bodies and Rspamd errors increment `fail_count`, but the command
      returns zero. HTTP 204 `declined` is counted as learned and is eligible
      for moving despite no model update. Automation can report a complete
      bootstrap when the corpus is incomplete.
    - Report learned/already/declined/failed separately, return nonzero for
      unresolved failures, and require an explicit policy for moving declines.

23. **CR-023 [MEDIUM][SECRETS][PARSING] Secret parsers disagree and rendered values are not escaped**
    - Locations: `unraid/bootstrap.sh:86-123`,
      `redis/redis.conf.template:19-23`,
      `rspamd/local.d/redis.conf.template:4-6`,
      `rspamd/local.d/worker-controller.inc.template:4-6`,
      `filter/filter.py:56-78`.
    - Bash strips everything after `#` even inside quotes, while Python/Compose
      can preserve a quoted `#`. Quotes and backslashes are then inserted
      literally into Redis/UCL strings. Valid-looking passwords can produce
      mismatched credentials or invalid configuration.
    - Use one strict dotenv parser plus target-format escaping, or enforce and
      document a safe generated alphabet. Add Bash/Python parity tests.

24. **CR-024 [MEDIUM][BOOTSTRAP][ROLLBACK] Standalone fallback installs a stale mixed version**
    - Locations: `unraid/bootstrap.sh:27-43`,
      `unraid/bootstrap.sh:66-84`, `unraid/bootstrap.version:1`.
    - The hard-coded no-checkout pin predates current configuration, and its
      fallback version is 7 while the tracked version is 8. Per-file activation
      has no bundle checksum, lock, whole-config validation, or rollback, so an
      interrupted refresh can leave mixed versions.
    - Release a versioned/checksummed bundle, stage and validate it as a unit,
      and update pin plus version from one source.

25. **CR-025 [MEDIUM][DOCUMENTATION][DEPLOYMENT] The generic-Linux procedure combines incompatible paths**
    - Locations: `README.md:326-344`, `deploy/vps-bootstrap.sh:5-11`,
      `docker-compose.yml:22-29`, `docker-compose.yml:59-66`.
    - The documented `/srv/spamfilter` override still uses the VPS wrapper's
      repository account path and ByteLord secret default, while root Compose
      mounts `/mnt/user/appdata/spamfilter`. Following the procedure does not
      deploy the files it just created.
    - Parameterize Compose, accounts, appdata, and secrets consistently and
      test the documented generic-host sequence.

26. **CR-026 [MEDIUM][AUTH][DOS] Login throttling is spoofable and unbounded**
    - Locations: `filter/dashboard.py:275-301`,
      `filter/dashboard.py:699-722`.
    - The application trusts arbitrary `X-Forwarded-For`; an unauthenticated
      client can vary it and usernames to evade per-pair lockout. Every unique
      pair remains in `_login_fails` because expiration occurs only when that
      exact key is reused. PBKDF2 work can occupy all four dashboard threads,
      and unbounded keys can OOM the shared mail-filter process.
    - Trust forwarding headers only from configured proxies; bound and
      periodically expire state; cap request/input sizes; add per-IP, per-user,
      and global limits at the reverse proxy and application.

27. **CR-027 [MEDIUM][AUTH][SIDE-CHANNEL] Legacy users have a strong timing oracle**
    - Locations: `filter/dashboard.py:183-200`.
    - Unknown/hashed users perform 600,000 PBKDF2 rounds; a valid legacy
      username uses only `compare_digest`. This reveals legacy usernames and
      permits much faster guessing, amplified by CR-026.
    - Remove/migrate plaintext legacy auth, or perform equivalent KDF work on
      every branch.

28. **CR-028 [MEDIUM][SESSION][DOCUMENTATION] Password changes do not revoke sessions**
    - Locations: `filter/dashboard.py:203-209`,
      `filter/dashboard.py:249-259`, `filter/dashboard.py:304-316`,
      `README.md:550-553`.
    - Flask's default session is a signed client-side cookie, not the documented
      server-side session. Authorization reloads the username/scope but does
      not bind the cookie to the password verifier, so changing a compromised
      password leaves stolen cookies valid for up to 24 hours.
    - Use server-side sessions or validate a credential-version/verifier
      fingerprint on each request; correct the documentation.

29. **CR-029 [MEDIUM][DASHBOARD][ACTIVATION] Dashboard configuration contracts contradict runtime**
    - Locations: `filter/dashboard.py:9-16`,
      `docker-compose.yml:69-79`, `filter/filter.py:2556-2571`,
      `README.md:561-587`, `unraid/spamfilter.xml:51`.
    - Module/Compose text advertises `DASHBOARD_USERS=name:hash`, but the parser
      correctly rejects records without `:scope`. Separately, the daemon starts
      the dashboard only if a user exists at process startup, so adding the
      first file user cannot make the listener appear without a restart despite
      the XML's “No restart needed” claim.
    - Document `name:hash:scope` everywhere. State that the first user requires
      restart, or always start a dormant listener that activates safely.

30. **CR-030 [MEDIUM][TESTS] Dashboard tests do not prove endpoint authorization or the production sibling query**
    - Locations: `filter/test_dashboard.py:15-92`,
      `filter/test_dashboard.py:153-195`; production query:
      `filter/dashboard.py:959-978`.
    - The sibling test copies SQL into the test instead of requesting
      `/messages`; production can regress while the copy stays green. No
      integration test proves per-route account scoping, admin-only Rspamd
      data, security headers/cookies, lockout, expiry, DB 503 behavior, or
      secret permissions.
    - Exercise authenticated endpoints against a temporary DB with two accounts
      and both scoped/admin users. Assert rendered output and all hardening
      contracts.

31. **CR-031 [MEDIUM][SUPPLY-CHAIN][CI] Runtime artifacts and test coverage are not reproducible**
    - Locations: `filter/Dockerfile:1-21`, `filter/requirements.txt:1-5`,
      `.github/workflows/build.yml:7-16`, `.github/workflows/build.yml:31-88`,
      `docker-compose.yml:17-54`, `deploy/bytelord-compose.yaml:29-61`.
    - Production deliberately runs Python 3.12 due to a Python-3.14/IMAPClient
      incompatibility, but CI tests only 3.14. Base/container tags, GitHub Action
      tags, transitive Python dependencies, and test pytest are mutable or
      unhashed. Deployment, bootstrap, and Rspamd changes are excluded by CI
      path filters and receive no ShellCheck/configtest validation.
    - Test the deployed 3.12 runtime (optionally keep 3.14), pin reviewed
      digests/SHAs and a hash-locked dependency set, and run native validators
      when deployment/configuration files change.

32. **CR-032 [MEDIUM][NETWORK][BOUNDARY] Backend services share the OAuth-facing network**
    - Locations: `deploy/bytelord-compose.yaml:28-90`,
      `rspamd/local.d/worker-normal.inc:1`,
      `rspamd/local.d/worker-controller.inc.template:4-12`.
    - Redis, Unbound, and both Rspamd workers share external `spamnet` with
      `email-oauth2-proxy`. A compromised peer can reach the unauthenticated
      scanner/DNS endpoints and probe password-protected state/controller
      services.
    - Put Redis, Unbound, and Rspamd on an internal backend network; attach only
      the filter to both the proxy-facing and backend networks.

33. **CR-033 [MEDIUM][HEALTH][LIFECYCLE] Health and startup checks do not represent service readiness**
    - Locations: `filter/filter.py:1532-1555`,
      `filter/filter.py:2590-2629`, `filter/Dockerfile:34-38`,
      `docker-compose.yml:17-58`, `deploy/bytelord-compose.yaml:29-65`.
    - The main watchdog refreshes the single heartbeat even when all account
      workers are hung; it restarts only dead threads. Dependencies have no
      health checks, and `depends_on` does not wait for Redis/DNS/Rspamd
      readiness. Docker can report healthy while no mailbox is being processed.
    - Track per-account progress and expose stale/dead status in health,
      metrics, and dashboard; add dependency health checks while retaining
      runtime reconnect/backoff.

## Low-severity findings

34. **CR-034 [LOW][DATABASE][PERFORMANCE] Dashboard pages repeatedly scan and observe mixed snapshots**
    - Locations: `filter/dashboard.py:324-329`,
      `filter/dashboard.py:745-785`, `filter/dashboard.py:959-978`,
      `filter/dashboard.py:1050-1093`; indexes:
      `filter/filter.py:639-640`, `filter/filter.py:672-680`.
    - Global event/time queries cannot efficiently use the account-leading
      index; message ordering uses an unindexed expression. Summary/accounts
      issue multiple statements without an explicit read transaction, so one
      page can combine different writer snapshots.
    - Confirm with `EXPLAIN QUERY PLAN`, add targeted indexes/pagination or a
      short cache, and wrap multi-query pages in one read transaction.

35. **CR-035 [LOW][DASHBOARD][CORRECTNESS] Score bands overlap at 8.0**
    - Locations: `filter/dashboard.py:949-957`,
      `filter/dashboard.py:990-994`.
    - A score of exactly 8 appears in both `spam` (`>= 8`) and `mid`
      (`BETWEEN 4 AND 8`) views.
    - Make the middle interval `>= 4 AND < 8` and add boundary tests.

36. **CR-036 [LOW][PRIVACY][LOGGING] Normal logs include mail content and unredacted response text**
    - Locations: `filter/filter.py:105-116`,
      `filter/filter.py:1196-1200`, `filter/filter.py:1854-1861`,
      `filter/bootstrap_train.py:83-97`.
    - Shadow mode logs subjects at INFO, bootstrap logs subjects/Message-IDs,
      and unexpected controller bodies bypass `redact_log()`. Central Docker
      logs can retain personal content or reflected credentials.
    - Remove subject/Message-ID from default logs (or hash them), make metadata
      opt-in debug output, and redact all external error bodies.

37. **CR-037 [LOW][SHUTDOWN] Compose termination grace is shorter than the implemented shutdown**
    - Locations: `filter/filter.py:2631-2636`, `docker-compose.yml:52-85`,
      `deploy/bytelord-compose.yaml:58-84`.
    - The process allows up to 180 seconds per thread to finish, but Compose
      defines no `stop_grace_period` (Docker normally sends SIGKILL much
      sooner). The intended orderly IMAP logout/DB shutdown therefore cannot be
      relied upon.
    - Set a bounded stack-level stop grace consistent with real network
      timeouts and join workers against one overall deadline.

38. **CR-038 [LOW][OPERATIONS][CAPACITY] Redis fail-closed capacity has no automated alert**
    - Locations: `redis/redis.conf.template:7-17`,
      `rspamd/local.d/classifier-bayes.conf:7`, `README.md:526-535`.
    - Persistent Bayes/state data has no expiry and Redis uses 1 GB
      `noeviction`. This intentionally protects learned data, but at capacity
      writes fail and training degrades. The README asks the operator to check
      memory manually; no service health/metric alerts on write failures or
      capacity.
    - Export Redis memory/write status, alert before the limit, and document a
      tested expansion/archival procedure.

## Cross-cutting test gaps

The highest-value missing tests are:

- Real `init_db()` migration from every historical schema and interruption at
  each migration phase.
- RFC-correct IMAP `*` behavior, arrival between searches, UIDVALIDITY changes,
  and missing FETCH data.
- Failure injection after score persistence, after server-side MOVE, and before
  DB reconciliation.
- Rspamd malformed/non-finite responses and distinct 401/403/429/5xx/network
  policies.
- Duplicate/NULL Message-ID retry isolation and ambiguous identical-body
  deliveries.
- Unknown keys, NaN/Infinity, timing bounds, and cross-layer TLS alias
  precedence.
- End-to-end Unraid/generic-host bootstrap with secret permissions and parser
  parity.
- Dashboard endpoint authorization/hardening, activation, rate-limit bounds,
  session revocation, and rendered sibling learns.
- CI assertions that test failure prevents image publication.

## Files reviewed

Runtime and tests:

- `filter/filter.py`, `filter/dashboard.py`, `filter/bootstrap_train.py`,
  `filter/Dockerfile`, `filter/requirements.txt`
- Every `filter/test_*.py` file:
  `test_connection.py`, `test_dashboard.py`, `test_env_overrides.py`,
  `test_fetch_discipline.py`, `test_inbox_bookmark.py`, `test_learn.py`,
  `test_log_redact.py`, `test_message_identity.py`, `test_rspamd_scan.py`,
  `test_secrets.py`, and `test_shadow_mode.py`

Configuration, deployment, and CI:

- `.env.example`, `.gitignore`, `accounts.yml.example`, `ca_profile.xml`
- `.github/workflows/build.yml`, `docker-compose.yml`,
  `deploy/bytelord-compose.yaml`, `deploy/vps-bootstrap.sh`
- `redis/redis.conf.template`
- All `rspamd/local.d/*` tracked configuration/template files
- `unraid/bootstrap.sh`, `unraid/bootstrap.version`, and all four Unraid XMLs

Architecture and requirements:

- `README.md`, `SESSION_HANDOFF.md`
- `design-arch/sliced_plan_code_review_fixes.md`
- `design-arch/spamfilter_discussion_and_code_review.md`
- `design-arch/slice1_hybrid_shadow_mode.md` through
  `design-arch/slice8_dashboard_hardening.md`

Non-source binary assets and the license were inventoried but are outside code
review scope.
