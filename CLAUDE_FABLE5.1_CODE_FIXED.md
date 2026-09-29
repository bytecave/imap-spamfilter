# Claude Fable 5.1 review — what was fixed

**Date:** 2026-09-29 (Pacific)
**Review:** [`CLAUDE_FABLE5.1_CODE_REVIEW.md`](CLAUDE_FABLE5.1_CODE_REVIEW.md), written against `main` at `f53586a`
**Result:** 32 findings. **25 were fixed in the tree**, each with regression tests or a config check. 7 are left as recommendations or operator decisions (see [Not fixed](#not-fixed--recommendations-and-operator-decisions)).
**Nothing is deployed.** The live containers still run the 2026-09-28 19:22 image and the old rspamd config. See [Deploying these fixes](#deploying-these-fixes).

## Contents

- [Fixes](#fixes)
- [Not fixed — recommendations and operator decisions](#not-fixed--recommendations-and-operator-decisions)
- [Deploying these fixes](#deploying-these-fixes)
- [What to test first](#what-to-test-first)
- [Validation](#validation)

## Fixes

Ordered by severity, as in the review. Each row gives the commit and the tests that prove it.

| ID | Sev | What changed | Commit | Tests |
|---|---|---|---|---|
| FABLE-CR-001 | High | Removed `expire = 0` from `rspamd/local.d/classifier-bayes.conf`. Any number there turns on rspamd's Bayes expiry, and 0 deleted about 374,700 rare tokens after they were learned. With the key gone, tokens never expire again (stock behaviour). `unraid/bootstrap.version` was bumped to 14, and the README and slice 7 were corrected. **Takes effect only after the file is copied to the live `local.d` and rspamd restarts.** Tokens already deleted are *not* restored. | `1a9d133` | `test_config_files.py` |
| FABLE-CR-002 | High | `_identical_copies_in_folder` no longer sends a Message-ID to IMAP SEARCH unless it is plain printable ASCII with no whitespace, `"`, `\`, `{`, `}`, `(` or `)`. Anything else counts as "no verified copy", which every caller already handles safely: the list drain MOVEs, the Train leftover is kept, and Train-Ham copies. | `f1f15c3` | `test_fable_review_fixes.py` (includes a fake IMAP server that honours `{n}` literals) |
| FABLE-CR-003 | High | `first_recipient` skips an address HTTP cannot carry and falls back to the mailbox. `rspamd_scan_detail` omits any `From`/`Rcpt` value that is not printable ASCII. rspamd still parses the MIME headers itself, and the CR-019 behaviour for normal mail is unchanged. | `e67a45f` | `test_fable_review_fixes.py` (includes a real HTTP round trip) |
| FABLE-CR-004 | Medium | `/checkv2` requests now carry `Pass: all`, so rspamd runs every rule instead of stopping at `reject = 15`. Covers the scan, the health probe and `explain_score.py`. **This changes the scores of high-scoring mail once deployed.** | `640bc55` | `test_rspamd_scan.py::test_scan_asks_rspamd_to_run_every_rule` |
| FABLE-CR-006 | Medium | The list editor now posts the list it loaded, from hidden `scope`/`kind` fields. The dropdown and radios only navigate, and Cancel reverts to the loaded list rather than the URL. A Save can no longer write one list's text into another. | `7071cac` | `test_dashboard.py::test_list_editor_posts_the_loaded_list_not_the_navigation_controls` |
| FABLE-CR-007 | Medium | Each editor page carries a fingerprint of the list and its sibling. The Save re-checks it inside its `BEGIN IMMEDIATE` transaction. If an Outlook drag or another save changed the list, it returns **409**, writes nothing, and shows the admin's text with the current fingerprint, so a second Save is a deliberate overwrite. | `7071cac` | `test_dashboard.py::test_stale_list_save_is_refused_and_writes_nothing` |
| FABLE-CR-008 | Medium | Oversize (>5 MiB) Train-Ham is COPYed to the Inbox server-side with no download. The drain keeps any ham message in Train-Ham until its Inbox copy exists, so an oversize or failed-COPY message is no longer archived to Trained-Ham and then trashed. | `f72a310` | `test_fable_review_fixes.py` (oversize, failed COPY, Train-Spam control) |
| FABLE-CR-009 | Medium | `scan_inbox` recognises a Train-Ham restore (body SHA against an `inbox_copied` row) *before* the Junk→Inbox revert check. A Train-Ham copy of mail that sat in Junk is therefore scored, list-checked (a block still junks it) and held as `ham_restored`, with no duplicate ham learn. Only the first Inbox arrival of that body counts, so a later drag back out of Junk still learns ham. The unreachable UIDPLUS branch was removed: imapclient returns `None` for UID COPY. The test double now returns `None` too. | `b26a641` | `test_fable_review_fixes.py` (Junk-origin hold, block list, later revert), updated `test_core_review_fixes.py` |
| FABLE-CR-010 | Medium | The ByteLord compose now tags the filter build `imap-spamfilter:bytelord`, a name no registry can serve. `docker compose pull` can no longer swap in the upstream author's public image. | `84db875` | `docker compose config` check |
| FABLE-CR-012 | Low | `parse_envelope` reads the Message-ID through `str()`. An 8-bit Message-ID no longer wipes the Subject and From, so an Allowlist/Blocklist drag of such mail records its sender. | `e186611` | `test_fable_review_fixes.py` |
| FABLE-CR-013 | Low | YAML and read errors in `accounts.yml` raise `ConfigError` with only the line and column. The offending line (often a password) is no longer quoted in logs or tracebacks. | `bf92750` | `test_fable_review_fixes.py` |
| FABLE-CR-014 | Low | The user-add helper refuses a blank account scope such as `,` or `|`, which used to become admin. | `82c47e1` | `test_dashboard.py::test_user_helper_*` |
| FABLE-CR-015 | Low | The CSRF check compares bytes, so a non-ASCII token gives 400, not 500. | `0afa484` | `test_dashboard.py::test_list_post_rejects_wrong_csrf_token_with_400` |
| FABLE-CR-016 | Low | The users file is written to a 0600 temp file, fsynced and renamed into place. A failure leaves the old file intact. | `82c47e1` | `test_dashboard.py::test_write_private_is_atomic_and_keeps_old_file_on_failure` |
| FABLE-CR-017 | Low | An undecodable users file is logged and treated as empty, and a non-ASCII hash is a failed login. Both fail closed instead of returning 500. | `0afa484` | `test_dashboard.py::test_undecodable_users_file_fails_closed` |
| FABLE-CR-018 | Low | The list textarea emits the newline HTML drops after `<textarea>`, so the error caret lands on the right line. | `7071cac` | `test_dashboard.py::test_list_error_line_survives_a_leading_blank_line` |
| FABLE-CR-019 | Low | The rspamd stats fetch uses `allow_redirects=False`, so it can never forward the controller `Password` header to another host. | `ea11375` | `test_dashboard.py::test_rspamd_stats_do_not_follow_redirects` |
| FABLE-CR-020 | Low | `bootstrap_train.py` now exits 1 when the source folder does not exist; it used to print `skip` and exit 0. In the read-only `--all-trained` re-feed, oversize mail counts as skipped, not failed. | `f9e867d` | `test_bootstrap_train.py` |
| FABLE-CR-021 | Low | The README points `bootstrap_train.py` at a folder of your own, with full paths, instead of the live Train-Ham folder, which would have bypassed the Inbox copy. | `75c9fb9` | docs |
| FABLE-CR-022 | Low | `explain_score.py --message-id` refuses a UIDVALIDITY mismatch and says when a UID is gone instead of calling it "over 5 MiB". An ambiguous Message-ID no longer also prints "not in DB". | `6ac1a26` | `test_explain_score.py` |
| FABLE-CR-023 | Low | A list-folder message waiting on a learn backoff waits for its retry time instead of being re-downloaded every pass. `list_imap_add` is logged only for a new entry, not every 30 s. | `4156d61` | `test_fable_review_fixes.py` |
| FABLE-CR-024 | Low | The generic `docker-compose.yml` gets `stop_grace_period: 90s`. `.gitignore` now covers `secrets/`, `.bootstrap.version` and `.bootstrap-stage.*/`. | `a2528b5` | `git check-ignore`, `docker compose config` |
| FABLE-CR-025 | Low | Added the missing dashboard tests: wrong CSRF token, an authenticated Save over 16 KiB, POST `/logout`, `/messages` escaping of mail fields, and the `list_dashboard_save` event. | `0afa484`, `1473c14` | `test_dashboard.py` |
| FABLE-CR-026 / 027 | Low | The orientation test command now mounts the whole repository. Stale docs were corrected: shadow "no writes", the "read-only" dashboard, and the hard-rules docstring. | `75c9fb9` and the docs commit | docs |

## Not fixed — recommendations and operator decisions

| ID | Why it was left | Recommendation |
|---|---|---|
| FABLE-CR-001 (recovery) | Restoring the deleted tokens means rebuilding the shared `bytelord` notebook. That is an operator decision, and the orientation forbids wiping Bayes during the review. | After deploying the config fix, decide whether to rebuild: back up Redis, clear the `bytelord` notebook **including its learn cache**, then run `bootstrap_train.py --all-trained`. A plain re-feed returns 208 and restores nothing. Retention has already moved older `rich_bytecave` Trained-* mail to Deleted Items, so move it back into Trained-* first if you want it relearned. |
| FABLE-CR-005 | Real Unbound recursion turns on Spamhaus/URIBL scoring on a live move-mode mailbox. That is a scoring change to schedule and watch. | Mount a recursion-only `forward-records.conf`, recreate Unbound, restart rspamd, and check `drill 2.0.0.127.zen.spamhaus.org`. Exact steps are in the review. |
| FABLE-CR-011 | This is a configuration-surface change, and every live account is M365. | Add a per-account `m365_auth_trust` (default true) before adding any non-M365 mailbox. |
| FABLE-CR-028 | Oversize mail skipping list routing is documented behaviour. | FETCH headers only for oversize UIDs and apply block/allow routing. |
| FABLE-CR-029 | Noise only; recovery already works. | Classify token-expiry reconnects as `conn_recycled`. |
| FABLE-CR-030 | Deploy change. | Set `DASHBOARD_COOKIE_SECURE: "1"` in the ByteLord compose. |
| FABLE-CR-031 | Policy/UX items. | Let a correct password bypass the per-user lockout, and reserve the account name `admin`. |
| FABLE-CR-032 | Hardening that needs rspamd/CI changes and a deploy. | Set `allow_file_and_shm_inputs = false`, use a separate controller `enable_password`, clean up `rbl.conf`, add a real per-account healthcheck, scope CI `packages: write`, keep the code root-owned in the image, and map a learn 404 to `already`. |

The generic `docker-compose.yml` still tags its build with the upstream name, because that file is the upstream-style install path. Only the ByteLord compose was changed (FABLE-CR-010).

## Deploying these fixes

Nothing below has been run. The operator decides when. Use the existing runbook pattern in `SESSION_HANDOFF.md`: diff first, back up, then check.

1. **Bayes expiry (FABLE-CR-001), most urgent.** Every hour it stays live deletes newly learned rare tokens.
   ```bash
   cd /opt/bytelord/projects/imap-spamfilter
   LIVE=/opt/bytelord/data/imap-spamfilter/rspamd/local.d
   cp "$LIVE/classifier-bayes.conf" "$LIVE/classifier-bayes.conf.bak.$(date +%Y%m%d-%H%M%S)"
   cp rspamd/local.d/classifier-bayes.conf "$LIVE/classifier-bayes.conf"
   docker exec spamfilter-rspamd rspamadm configtest
   docker restart spamfilter-rspamd
   ```
   **Expect:** `syntax OK`; after the restart, `docker logs --since 10m spamfilter-rspamd 2>&1 | grep -c "finished expiry"` shows **0**, because the module is off. Redis is not touched.
2. **Filter and dashboard code** (FABLE-CR-002…004, 006…009, 012…023): rebuild and recreate `spamfilter` only.
3. **Compose image name (FABLE-CR-010):** sync `deploy/bytelord-compose.yaml` to `/opt/bytelord/compose/imap-spamfilter/compose.yaml` (diff, back up, `cp`, `config`) before the rebuild in step 2. The build then produces `imap-spamfilter:bytelord`. The old `ghcr.io/marcelverdult/imap-spamfilter:latest` tag can be removed with `docker image rm` once the new container is healthy.

## What to test first

See the matching table in `SESSION_HANDOFF.md`. In short:

1. **Bayes expiry is off and tokens stay.** Record the `RS*_*` key count, Train-Spam a few messages, and check that the count only grows.
2. **Scores after `Pass: all`.** High-scoring mail may move up or down. Watch mail near 8 and 4 on `rich_bytecave` for a day.
3. **Train-Ham of a Junk message** comes back to the Inbox and stays, and shows `ham_restored` with a score. The same with a block-listed sender goes back to Junk in move mode.
4. **List editor.** Make a bad edit, fix it, and Save to the same list. Open two tabs and save in one, then the other: the second gets 409.
5. **Oversize Train-Ham** (a >5 MiB message) comes back to the Inbox.

## Validation

- Docker `python:3.12-slim` with the whole repository mounted: **487 passed** (baseline 429).
- **47 of the new tests fail against the original `f53586a` code** (checked in a separate worktree) and pass now. The remaining new tests are controls or coverage for behaviour that was already correct: ordinary Train-Ham COPY failure, oversize Train-Spam, wrong ASCII CSRF, the 16 KiB Save, logout, escaping, and the audit event.
- The High findings were reproduced before fixing:
  - Bayes expiry: from the live rspamd log.
  - Message-ID literal: against an IMAP server that honours literals.
  - 8-bit headers: against a real HTTP connection.
- `docker compose config` is valid for both compose files. `node` parses `lists.js`.
- Live checks during the review were read-only: SQLite `?mode=ro`, `docker logs`, one config file inside the Unbound container, and two non-secret environment variables.
