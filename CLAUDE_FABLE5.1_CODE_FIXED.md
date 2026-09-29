# Claude Fable 5.1 review — what was fixed

**Date:** 2026-09-29 (Pacific)
**Review:** [`CLAUDE_FABLE5.1_CODE_REVIEW.md`](CLAUDE_FABLE5.1_CODE_REVIEW.md), written against `main` at `f53586a`
**Result:** 32 findings. **25 were fixed in the review pass**, and on the operator's go-ahead **4 more (005, 011, 030, part of 032) in a second pass the same day**, each with tests or a config check. The rest are left as recommendations or won't-fix decisions (see [Not fixed](#not-fixed--recommendations-and-operator-decisions)).
**Nothing is deployed.** The live containers still run the 2026-09-28 19:22 image and the old rspamd config. See [Deploying these fixes](#deploying-these-fixes).

## Contents

- [Fixes](#fixes)
- [Second pass: operator-approved follow-ups](#second-pass-operator-approved-follow-ups)
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

## Second pass: operator-approved follow-ups

After reading the plain-language summary of the open items, the operator asked for these to be fixed and bundled with the rest. Also on 2026-09-29:

| ID | What changed | Commit | Tests / checks |
|---|---|---|---|
| FABLE-CR-005 | Both compose files mount `unbound/forward-records.conf`, which has no `forward-zone`, over the image's Cloudflare forwarder. Unbound now resolves from the root servers, so Spamhaus, URIBL and SURBL answer. Checked on this VPS with a throwaway container: ZEN returns its test codes `127.0.0.2/.4/.10` after a few seconds of cache warm-up, DBL returns `127.0.1.2`, and SpamCop, URIBL and SURBL all answer. The live forwarder returns `127.255.255.254` ("public resolver refused") for Spamhaus. No Spamhaus key is needed. | `75909a1` | `docker compose config`; live comparison |
| FABLE-CR-011 | New per-account `m365_auth_trust` (default **true**, so the ten Microsoft 365 accounts are unchanged). When false, the account ignores `Authentication-Results` entirely: no bucket-B cancellation, and no Microsoft spoof verdict for rescue or the allowlist flag. Set it false for any future mailbox Microsoft 365 does not deliver to. | `2239d6c` | `test_fable_review_fixes.py` (config, scan both ways, scan path, rescue) |
| FABLE-CR-030 | `DASHBOARD_COOKIE_SECURE: "1"` in the ByteLord compose. | `8b82a17` | `docker compose config` |
| FABLE-CR-032 (part) | Configuration only; no rspamd code touched. `allow_file_and_shm_inputs = false` on the normal, controller and proxy workers (the proxy on `*:11332` is also reachable from spamnet). In `rbl.conf`, the unweighted duplicate Spamhaus ZEN rule is removed: the stock rule has per-listing symbols and weights and now works. Abusix is removed because it needs an account key; the comment says how to add it back. SpamCop is kept, checks `Received:` hops, and gets weight **1.5** in the new `rbl_group.conf`. Bootstrap installs the two new files. Validated with `rspamadm configtest` in a throwaway rspamd 4.2.0. | `2acdc08` | `test_config_files.py` (workers, RBL rules, weights, bootstrap file list) |
| FABLE-CR-032 (part) | `rspamd_learn` treats rspamd 4.2's `404 "… has been already learned as …"` (from its learn cache) as `already`, so that message is no longer retried forever. Other 404s are still errors. | `1ff0efb` | `test_fable_review_fixes.py` |

**Won't fix (operator decision):** the separate controller `enable_password`, a per-account healthcheck, CI token scope, image file ownership (all FABLE-CR-032), and `DASHBOARD_TRUSTED_PROXIES` (OPUS-CR-023). The operator's rule for this project: **never change rspamd code**, because it is replaced on every upstream update. Configuration in `local.d` is fine.

## Not fixed — recommendations and operator decisions

| ID | Why it was left | Recommendation |
|---|---|---|
| FABLE-CR-001 (recovery) | Restoring the deleted tokens means rebuilding the shared `bytelord` notebook. That is an operator decision, and the orientation forbids wiping Bayes during the review. | After deploying the config fix, decide whether to rebuild: back up Redis, clear the `bytelord` notebook **including its learn cache**, then run `bootstrap_train.py --all-trained`. A plain re-feed returns 208 and restores nothing. Retention has already moved older `rich_bytecave` Trained-* mail to Deleted Items, so move it back into Trained-* first if you want it relearned. |
| FABLE-CR-028 | Oversize mail skipping list routing is documented behaviour. | FETCH headers only for oversize UIDs and apply block/allow routing. |
| FABLE-CR-029 | Noise only; recovery already works. | Classify token-expiry reconnects as `conn_recycled`. |
| FABLE-CR-031 | Policy/UX items. | Let a correct password bypass the per-user lockout, and reserve the account name `admin`. |
| FABLE-CR-032 (rest) | Won't fix, by operator decision (see the second pass above). | — |

The generic `docker-compose.yml` still tags its build with the upstream name, because that file is the upstream-style install path. Only the ByteLord compose was changed (FABLE-CR-010).

## Deploying these fixes

Nothing below has been run; the operator decides when. It follows the runbook pattern in `SESSION_HANDOFF.md`: diff first, back up, then check. Run it from `/opt/bytelord/projects/imap-spamfilter` after `git pull`. Redis is never restarted.

1. **Sync the compose file.** This carries the Unbound mount, the Secure cookie and the new image name.
   ```bash
   C=/opt/bytelord/compose/imap-spamfilter/compose.yaml
   diff -u "$C" deploy/bytelord-compose.yaml     # expect only those three changes
   cp "$C" "$C.bak.$(date +%Y%m%d-%H%M%S)" && cp deploy/bytelord-compose.yaml "$C"
   docker compose -f "$C" config >/dev/null && echo OK
   ```
2. **Recreate Unbound** (FABLE-CR-005) and check that Spamhaus answers.
   ```bash
   docker compose -f "$C" up -d --force-recreate --no-deps spamfilter-unbound
   sleep 10; docker exec spamfilter-unbound drill 2.0.0.127.zen.spamhaus.org @127.0.0.1 | grep -A4 "ANSWER SECTION"
   ```
   **Expect:** `127.0.0.2`, `127.0.0.4`, `127.0.0.10`. The first query or two can SERVFAIL while the cache warms; repeat. `127.255.255.254` means Spamhaus still sees a public resolver; stop and report.
3. **Install the rspamd config** (FABLE-CR-001 and 032). The live stamp is `9`, because earlier updates were copied by hand, so copy by hand again.
   ```bash
   LIVE=/opt/bytelord/data/imap-spamfilter/rspamd/local.d
   cp -a "$LIVE" "$LIVE.bak.$(date +%Y%m%d-%H%M%S)"
   for f in classifier-bayes.conf rbl.conf rbl_group.conf worker-normal.inc worker-proxy.inc \
            actions.conf worker-controller.inc.template; do
     cp "rspamd/local.d/$f" "$LIVE/$f"; chmod 644 "$LIVE/$f"
   done
   grep -q '^allow_file_and_shm_inputs' "$LIVE/worker-controller.inc" \
     || echo 'allow_file_and_shm_inputs = false;' >> "$LIVE/worker-controller.inc"
   docker exec spamfilter-rspamd rspamadm configtest      # expect: syntax OK
   docker restart spamfilter-rspamd                       # also picks up the new Unbound address
   ```
   `worker-controller.inc` holds the rendered password, so append the one line as shown; do not print or copy the file.
   (`actions.conf` differs from the repo only in comments, and the `.template` file is not read by rspamd. Both are copied so the live folder matches the repo afterwards.)
   **Expect** after a few minutes:
   - `docker logs --since 10m spamfilter-rspamd 2>&1 | grep -c "finished expiry"` → **0**;
   - `docker exec spamfilter-rspamd rspamadm configdump worker | grep -c "allow_file_and_shm_inputs = false"` → **3**.

   *Alternative:* `bash deploy/vps-bootstrap.sh` refreshes every `local.d` file and re-renders the secret configs, because the stamp goes 9 → 14. Use it only if you want bootstrap to own these files again.
4. **Rebuild and recreate the filter.** This carries every `filter/` fix, including `Pass: all` and the "already learned" 404.
   ```bash
   export SPAMFILTER_UID=1001 SPAMFILTER_GID=1001
   docker compose -f "$C" build spamfilter
   docker compose -f "$C" up -d --force-recreate --no-deps spamfilter
   docker inspect -f '{{.Config.Image}}' spamfilter   # imap-spamfilter:bytelord
   ```
   Once it is healthy, the old tag can go: `docker image rm ghcr.io/marcelverdult/imap-spamfilter:latest`.

**Scores will move after steps 2–4.** Spamhaus, SpamCop and the URI blocklists start adding points, and `Pass: all` lets every rule run. The operator chose to deploy without a watch period. The dashboard's Messages page (score bands `8-19` and `≥20`) and `explain_score.py` show which symbols changed.

## What to test first

See the matching table in `SESSION_HANDOFF.md`. In short:

1. **Bayes expiry is off and tokens stay.** Record the `RS*_*` key count, Train-Spam a few messages, and check that the count only grows.
2. **Scores after `Pass: all`.** High-scoring mail may move up or down. Watch mail near 8 and 4 on `rich_bytecave` for a day.
3. **Train-Ham of a Junk message** comes back to the Inbox and stays, and shows `ham_restored` with a score. The same with a block-listed sender goes back to Junk in move mode.
4. **List editor.** Make a bad edit, fix it, and Save to the same list. Open two tabs and save in one, then the other: the second gets 409.
5. **Oversize Train-Ham** (a >5 MiB message) comes back to the Inbox.

## Validation

- Docker `python:3.12-slim` with the whole repository mounted: **487 passed** after the review pass and **501 passed** after the second pass (baseline 429).
- **47 of the new tests fail against the original `f53586a` code** (checked in a separate worktree) and pass now. The remaining new tests are controls or coverage for behaviour that was already correct: ordinary Train-Ham COPY failure, oversize Train-Spam, wrong ASCII CSRF, the 16 KiB Save, logout, escaping, and the audit event.
- The High findings were reproduced before fixing:
  - Bayes expiry: from the live rspamd log.
  - Message-ID literal: against an IMAP server that honours literals.
  - 8-bit headers: against a real HTTP connection.
- `docker compose config` is valid for both compose files. `node` parses `lists.js`.
- Live checks during the review were read-only: SQLite `?mode=ro`, `docker logs`, one config file inside the Unbound container, and two non-secret environment variables.
