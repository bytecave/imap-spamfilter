# Claude Opus 5.5 — Extra Code Review: Fixes Applied

Companion to [`CLAUDE_OPUS5.5_EXTRA_CODE_REVIEW.md`](CLAUDE_OPUS5.5_EXTRA_CODE_REVIEW.md).
I rechecked each finding against the code before fixing it, worked highest severity first, and gave every fix its own commit and regression tests.
Validation used Python 3.12 with `filter/requirements.txt` plus `pytest==8.4.2` (`cd filter && python -m pytest -q`).

## Fixes

### OPUS-CR-001 — List-drain de-dup no longer expunges on a Message-ID match alone (High)
`_message_ids_in_folder` is replaced by `_identical_copies_in_folder`. It fetches the destination's Message-ID candidates under the 5 MiB cap and counts a copy only if its body SHA-256 matches the dragged message. Anything unverified (collision, oversize, fetch error) is now MOVEd, never UID-EXPUNGEd, so a Message-ID collision can at worst leave a duplicate instead of deleting mail.
Tests: `test_drain_message_id_collision_moves_instead_of_expunging[allow|block]` and `test_drain_oversize_destination_candidate_is_not_proof_of_copy`; the existing leftover-cleanup test still passes.

### OPUS-CR-002 — A poison message no longer halts scanning forever (High)
`AccountState` now counts consecutive failures per exact IMAP object. After `SCAN_POISON_ATTEMPTS` (5) failures spanning at least `SCAN_POISON_MIN_AGE_S` (600 s), the UID is given up: `our_action='scan_giveup'`, a `scan_giveup` event, left in place, and the bookmark moves past it. A scan failure only leads to give-up if a tiny synthetic `/checkv2` probe succeeds on **two consecutive failing passes**, so a real rspamd outage (or rspamd recovering right after a failed scan) still halts as slice 3 requires; a repeatedly empty body gives up without a probe. This applies to both `scan_inbox` and `poll_junk`, and catch-up skips `scan_giveup` rows.
Tests: `test_poison_inbox_uid_is_given_up_and_later_mail_scored`, `test_rspamd_outage_never_gives_up`, `test_without_state_scan_failure_keeps_historic_halt`, `test_repeated_empty_body_is_given_up_without_probe`, `test_poison_junk_uid_does_not_block_later_user_move_learn`, `test_probe_recovering_right_after_a_failed_scan_is_not_poison`.

### OPUS-CR-003 — Provider-Junk rescue honors Microsoft's spoof verdict (High; fixed for rescue, policy left open for Inbox)
I split out `_trusted_m365_ar()` (the same outermost-header trust rule bucket B uses) and added `m365_spoof_verdict()`: `compauth=fail`, or `dmarc=fail` when there is no compauth token. `poll_junk` checks it before queueing a rescue (logging `rescue_skipped`, so shadow no longer reports spoofs as `would_rescue`), and `execute_due_rescues` checks it again before MOVE (`pending_rescue_canceled m365_spoof_verdict`). This only ever *prevents* a MOVE, so allowlisted spoofs of vendor or internal addresses stay in Junk, where Microsoft put them. Allow hits that keep an Inbox spoof out of Junk are unchanged; that is an operator policy decision (see the review).
Tests: `test_m365_spoof_verdict[*]`, `test_allowlisted_spoof_is_not_rescued`, `test_spoof_is_not_reported_as_would_rescue_in_shadow`, `test_allowlisted_compauth_pass_is_still_rescued`, `test_due_rescue_rechecks_spoof_verdict`.

### OPUS-CR-005 — Dashboard list saves above 16 KiB now work in production (Medium)
`dashboard.start()` passes `max_request_body_size=LIST_POST_MAX` (256 KiB) to waitress. That is the only server-level cap that lets the slice-12 list route receive a full 1,000-entry Save. Flask's app-wide `MAX_CONTENT_LENGTH` still holds `/login` and every other route to 16 KiB, so slice 8's login bound is unchanged. I updated the old test that pinned the conflicting 16 KiB server cap and added an end-to-end test through a real waitress server.
Tests: `test_waitress_rejects_large_body_before_wsgi` (updated), `test_real_waitress_accepts_large_list_post_but_not_large_login` (new: 700 addresses ≈ 22 KiB reaches Flask; a >16 KiB `/login` body is still 413).

### OPUS-CR-006 — Scan-time Bayes identity now matches learning (Medium; latent on ByteLord)
`rspamd_scan_detail` always prepends `Delivered-To: <bayes_user or recipient>`, the same prefix `rspamd_learn` uses. The HTTP `Rcpt` choice is unchanged, so no other Rspamd rules shift. I confirmed in the rspamd 4.2.0 source that the first MIME `Delivered-To:` outranks `Rcpt` for the per-user Bayes key, so messages that already carry one (Postfix, Dovecot, Gmail) were classified against the wrong notebook for address/default identities. ByteLord's bare `bytelord` path is byte-for-byte unchanged.
Tests: `test_address_bayes_user_stays_in_rcpt` (updated to expect the prefix), `test_scan_identity_precedes_message_delivered_to`, `test_scan_without_bayes_user_prefixes_recipient_identity`.

### OPUS-CR-007 — Re-junking a filter-rescued message is learned as spam (Medium)
`rescued_to_inbox` is no longer in `_FILTER_OWNED_JUNK_ACTIONS`. A completed rescue means the filter moved that copy *out* of Junk, so the same bytes arriving in Junk again are the user correcting the filter. The rescued row (`current_folder=INBOX`) now counts as an Inbox sibling and follows the normal user-move spam-learn path. Filter-initiated moves (`pending_move`/`moved_to_junk`) and in-flight rescues (`pending_rescue`) are still excluded, so the rescue itself never trains.
Tests: `test_user_rejunk_of_rescued_message_is_learned`, `test_filter_move_of_rescued_message_is_still_not_learned`.

### OPUS-CR-008 — Rescue no longer bounces the user's own old mail back out of Junk (Medium)
`_rescue_blocker()` now also refuses a rescue when the Junk message's INTERNALDATE is older than `RESCUE_MAX_AGE_S` (3 days). IMAP MOVE keeps INTERNALDATE, so an old message that just appeared in Junk was moved there by the user (from Archive, or pre-install Inbox mail with no fingerprint), not delivered by Microsoft. It gets a `rescue_skipped old_internaldate` event instead of a MOVE to Inbox. Fresh provider deliveries and messages with no INTERNALDATE behave as before. The age check applies when the rescue is queued, not when it runs, so a long `move_grace_seconds` cannot cancel valid rescues.
Tests: `test_rescue_respects_internaldate_age[90 days → no rescue | 1 hour → rescue]`; existing rescue tests (no INTERNALDATE) unchanged.

### OPUS-CR-009 — Junk retention keeps pending, unseen, and allowlisted mail (Medium)
`_sweep_folder_to_trash` now, for Junk:
- only trashes UIDs at or below the Junk scan bookmark, and skips the Junk sweep until `poll_junk` has set that bookmark, so a message the user just dragged in is learned before it can age out;
- skips rows with `pending_learn` (in every swept folder);
- skips Junk rows marked `allowlisted` or `pending_rescue`.

It also applies the 500-per-pass cap *after* these exclusions. `poll_junk` marks allow-hit provider-Junk as `allowlisted` (unless the rescue is blocked as a spoof or old mail), so in `flag` mode R&J allowlisted mail in Junk is no longer sent to Trash after `junk_retention_days`. `execute_due_rescues` clears `pending_rescue` when it cancels on score.
Tests: `test_junk_retention_waits_for_poll_junk_bookmark`, `test_junk_retention_skips_pending_allowlisted_and_rescue_rows`, `test_poll_junk_marks_allowlisted_provider_junk`, `test_canceled_rescue_does_not_stay_pending`; the existing flag/move retention tests still pass.

### OPUS-CR-010 — Mail without a Message-ID is filtered like any other (Medium)
`scan_inbox` and `poll_junk` no longer skip messages without a Message-ID. The `no_message_id` audit event is still logged (once, when the UID is first seen), but the message is now stored (`message_id` NULL), scored, list-matched, flagged or queued for move, and learned from on user moves. `pending_move.message_id` is `NOT NULL`, so it stores `""`, and the due-move/rescue executors turn that back into NULL for events. This supersedes the "no Message-ID → permanent skip" rows in slices 3 and 5, which dated from when Message-ID was the primary key.
Tests: `test_inbox_without_message_id_is_scored_and_routed[flag|move]`, `test_user_move_without_message_id_is_learned`.

### OPUS-CR-011 — SQLite transactions take the write lock up front (Medium)
`Db.tx()` now issues `BEGIN IMMEDIATE`. In WAL mode, a deferred transaction that reads first (`log_event`'s subject lookup, `_set_learn_retry`) fails *at once* with `SQLITE_BUSY` when it upgrades to a write after another of the ~11 connections committed; the 30 s busy handler doesn't apply to a stale snapshot. That error used to reach `_run_account`'s generic handler and force an IMAP reconnect, sometimes right after a MOVE whose DB update was then lost. `IMMEDIATE` waits on the busy timeout instead.
Test: `test_tx_holds_write_lock_from_begin` (fails with deferred `BEGIN`, passes with `IMMEDIATE`).

### OPUS-CR-012 — Reconnect backoff no longer resets on a bare connect (Medium)
`_run_account` now resets `backoff` to the 5 s minimum only after a full pass (drains → scan → moves → rescues → junk poll → retention) has completed, just before the IDLE wait. It used to reset right after connecting, so a failure that recurs every pass (for example a server error on one message) re-logged into Microsoft 365 through the OAuth proxy every ~5 s indefinitely. The backoff now grows 5 → 10 → 20 … 300 s. I also made the dashboard's waitress end-to-end test shut its server thread down deterministically.
Test: `test_reconnect_backoff_grows_when_every_pass_fails` (the old code gives gaps of 5, 5, 5).

### OPUS-CR-013 — The learn budget is checked before bodies are downloaded (Medium)
New `_learn_budget()` = `max_learns_per_hour` minus learns recorded in the last hour.
- `_drain_train_folder` now fetches at most `min(max_train_per_run, budget)` UIDs, and none once the budget is used up.
- `process_pending_learns` stops before the next `BODY.PEEK[]` when the budget reaches 0. It records DB-only retry state on the remaining rows, the same way `try_learn`'s rate refusal did, so `prune_stale_pending_learn` still keeps them.

Before, a 500-message Train-Spam drop, or a mass Inbox→Junk move, was fully re-downloaded through the proxy on every backoff tick just to be refused after 50 learns.
Tests: `test_train_drain_fetches_no_bodies_when_budget_exhausted`, `test_train_drain_fetches_only_what_budget_allows`, `test_pending_learns_defer_without_fetch_when_budget_exhausted`.

### OPUS-CR-014 — Train-* never re-MOVEs a leftover copy (Medium; Inbox→Junk still needs live verification)
`_drain_train_folder` skips any Train-* UID whose DB row already shows `current_folder == Trained-*`. That means the filter learned and MOVEd it, but the server kept the source (Exchange MOVE-as-COPY, which the 2026-09-21 list-drain code already handles). Before, every pass (~30 s) MOVEd the same leftover into Trained-* again. It logs one warning per account/folder per process and **does not expunge**. Deleting outside the list-folder exception stays an operator decision.
**Not changed (verify live before `move` mode):** whether Exchange leaves the source copy for `execute_due_moves` (Inbox→Junk) and `execute_due_rescues`. If it does, spam would stay visible in Inbox. See the handoff checklist.
Test: `test_train_leftover_after_move_as_copy_is_not_moved_twice` (the old code moves the leftover again on every pass).

### OPUS-CR-016 — `pending_move_canceled` is logged only for a real cancellation (Low)
`Db.drop_pending_move` now returns the number of rows it deleted, and the `scan_inbox` allow branch logs `pending_move_canceled` only when a queued move was actually dropped. Before, every allowlisted Inbox message produced a phantom cancellation event, which hid real ones in the audit trail. The `execute_due_moves` allow re-check always has a real pending row and still logs it.
Test: `test_allow_hit_without_pending_move_logs_no_cancellation`; ChatGPT CR-001 cancellation tests still pass.

### OPUS-CR-017 — Bootstrap renders secret configs owner-only from the first byte (Low)
`unraid/bootstrap.sh` `render_subst` now runs awk in a `umask 077` subshell. The rendered `worker-controller.inc` and both Redis configs are created 0600 and only then widened to the intended 0640 by `verify_secret_file`; before, under the caller's umask (usually 022) they were briefly world-readable. The single-file-paste fallback version now matches `unraid/bootstrap.version` (10, not 9), and a test keeps them in lockstep.
Tests: `test_render_subst_creates_rendered_secret_owner_only` (extracts and runs the real bash function under umask 022), `test_bootstrap_fallback_version_matches_version_file`. `bash -n` is clean.

### OPUS-CR-015 — Core learning and move-mode paths now have tests (Medium; partially closed)
New tests exercise paths the suite never ran:
- the non-keyword user Inbox→Junk move → grace → `learn_spam`;
- Junk→Inbox revert → `pending_ham` → `learn_ham`, and `$NotJunk` learning immediately;
- a pending spam learn whose message left Junk during grace → `pending_lost`;
- UNSEEN over the cap → safe-mode "all" with no bookmark advance, then automatic exit;
- `execute_due_moves` honoring the remaining hourly move quota.

Together with the per-fix tests, `filter.py` branch coverage rose from **72% to 78%** (total 75% → 79%), and the suite from 349 to 396 tests. The shared `CapIMAP`/`RecordingIMAP` fakes return `FLAGS` for UIDs that don't exist, unlike a real server; the lost-UID test uses a realistic fake instead, and this is worth fixing in the shared fakes. Still recommended (not added): a scripted `_run_account` call-order test, and retention SEARCH/MOVE failure branches.
Tests: `test_user_inbox_to_junk_without_keyword_learns_after_grace`, `test_junk_to_inbox_revert_learns_ham_after_grace`, `test_notjunk_keyword_revert_learns_immediately`, `test_pending_spam_moved_out_during_grace_is_lost_not_learned`, `test_unseen_over_cap_enters_and_leaves_safe_mode`, `test_due_moves_respect_remaining_hourly_quota`.

### OPUS-CR-028 (partial) and CR-019 (docs only) — README matches the code
The README now:
- lists the nine managed folders (was "seven");
- documents the rescue spoof/age guards, the Junk-retention exclusions, poison give-up, and filtering of mail without a Message-ID;
- describes the Bayes identity accurately: `Delivered-To` is always prepended, an address is also `Rcpt`, and for a bare name `Rcpt` is the message's **first To/Cc recipient**, not "the mailbox" as it said before.

Whether `Rcpt` *should* be the mailbox (CR-019) is a scoring decision I left for the operator. The slice 9–12 status tables and their pre-2026-09-21 "skip /checkv2" wording are unchanged, since those design docs are the operator's record.

### Follow-up requested by the operator (2026-09-25)

- **Allowlisted spoof suspects are flagged (CR-003, Inbox side).** Allow still wins, so the message stays in Inbox. If Microsoft's trusted outermost AR marks it as spoofed, the filter logs `allowlisted_spoof_suspect` and, in `flag`/`move` mode, sets IMAP `\Flagged` (the red follow-up flag in Outlook/OWA). Shadow only logs it, because Inbox stays read-only there. A flag failure is logged and never stops the scan. Tests: `test_allowlisted_spoof_suspect_is_flagged_and_kept[flag|move]`, `test_allowlisted_spoof_suspect_shadow_only_logs`, `test_allowlisted_clean_auth_is_not_flagged`.
- **CR-022 (partial), `deploy/bytelord-compose.yaml`:** `TZ: America/Los_Angeles` (US Pacific, auto PST/PDT) and `stop_grace_period: 90s` on `spamfilter`, so Docker doesn't kill the filter mid-MOVE after the default 10 s. `env_file` is deliberately unchanged (see below). These only take effect once the file is copied to the live compose path.

## Not fixed — recommendations for the operator

These passed verification but are policy, scoring, or ops decisions, or too minor to justify the churn:

| ID | Why it was not changed here |
|---|---|
| OPUS-CR-004 (High) | Neural autotraining on unadjusted scores is a **scoring-policy and Redis-data decision**. The recommended steps are in the review (set `train { autotrain = false; }` or `frozen = true;` in `rspamd/local.d/neural.conf`, bump `unraid/bootstrap.version`, and after an explicit go-ahead delete only the `rn_*` Redis keys). Nothing was changed in config or data. |
| OPUS-CR-003 (Inbox side) | Allow still keeps a Microsoft-flagged spoof in Inbox (moving it to Junk could hide mail the user explicitly trusts). The message is now flagged instead; see the follow-up above. |
| OPUS-CR-014 (Inbox→Junk, rescues) | Whether Exchange leaves a source copy after `UID MOVE` from Inbox or Junk has to be observed on the live tenant before deciding to detect or expunge. |
| OPUS-CR-018 | Dashboard catch-rate semantics (needs a folder in the `scan` event detail, or a relabel). |
| OPUS-CR-019 | Whether HTTP `Rcpt` should be the mailbox. That is a scoring change (`FORGED_RECIPIENTS` on list and BCC mail); the README now documents the real behavior. |
| OPUS-CR-020 | Double learns after Allow/Block drags. Harmless to Bayes (Rspamd 208) and only inflates counters. |
| OPUS-CR-021, 024, 025, 026, 027 | Low-impact validation, diagnostics, and UX items; details and recommended changes are in the review. |
| OPUS-CR-022 (`env_file`), 023 | `env_file` stays: it only exposes the passwords to people who already control Docker (effectively root), and removing it could break anything else the operator keeps in that file. `DASHBOARD_TRUSTED_PROXIES` needs the live Docker bridge gateway IP. |
| OPUS-CR-028 (rest) | The slice 9–12 status tables and superseded "skip /checkv2" text belong to the operator's design record. |

## Final validation

- `cd filter && python -m pytest -q` → **401 passed** (baseline was 349).
- Branch coverage: `filter.py` 72% → 78%; total 75% → 79%.
- `python -m compileall -q filter` is clean; `bash -n deploy/*.sh unraid/*.sh` is clean.
- **Every fix-specific regression test fails against the original `ff9461e` code**, either on its assertions or, for the new helpers (`m365_spoof_verdict`, the poison constants), because they don't exist yet. For example, the backoff test sees gaps `[5, 5, 5]` and the list-drain collision test sees an EXPUNGE with no MOVE. The review's reproduction script confirmed CR-002's old behavior (bookmark stuck after 20 passes). The six CR-015 coverage tests and the "unchanged behavior" controls pass on both versions, as intended.
- There was no live IMAP or Rspamd here: everything was verified with the repo's IMAP/Rspamd fakes and against upstream Rspamd 4.2.0, waitress 3.0.2, and imapclient 3.1.0 source. The live-verification checklist is in `SESSION_HANDOFF.md`.
