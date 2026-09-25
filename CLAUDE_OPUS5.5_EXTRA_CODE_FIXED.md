# Claude Opus 5.5 — Extra Code Review: Fixes Applied

Companion to [`CLAUDE_OPUS5.5_EXTRA_CODE_REVIEW.md`](CLAUDE_OPUS5.5_EXTRA_CODE_REVIEW.md).
I rechecked each finding against the code before fixing it, worked highest severity first, and gave every fix its own commit and regression tests.
Validation used Python 3.12 with `filter/requirements.txt` plus `pytest==8.4.2` (`cd filter && python -m pytest -q`).

## Fixes

### OPUS-CR-001 — List-drain de-dup no longer expunges on a Message-ID match alone (High)
`_message_ids_in_folder` is replaced by `_identical_copies_in_folder`. It fetches the destination's Message-ID candidates under the 5 MiB cap and counts a copy only if its body SHA-256 matches the dragged message. Anything unverified (collision, oversize, fetch error) is now MOVEd, never UID-EXPUNGEd, so a Message-ID collision can at worst leave a duplicate instead of deleting mail.
Tests: `test_drain_message_id_collision_moves_instead_of_expunging[allow|block]` and `test_drain_oversize_destination_candidate_is_not_proof_of_copy`; the existing leftover-cleanup test still passes.

### OPUS-CR-002 — A poison message no longer halts scanning forever (High)
`AccountState` now counts consecutive failures per exact IMAP object. After `SCAN_POISON_ATTEMPTS` (5) failures spanning at least `SCAN_POISON_MIN_AGE_S` (600 s), the UID is given up: `our_action='scan_giveup'`, a `scan_giveup` event, left in place, and the bookmark moves past it. A scan failure only counts toward give-up if a tiny synthetic `/checkv2` probe succeeds, so a real rspamd outage still halts as slice 3 requires; a repeatedly empty body gives up without a probe. This applies to both `scan_inbox` and `poll_junk`, and catch-up skips `scan_giveup` rows.
Tests: `test_poison_inbox_uid_is_given_up_and_later_mail_scored`, `test_rspamd_outage_never_gives_up`, `test_without_state_scan_failure_keeps_historic_halt`, `test_repeated_empty_body_is_given_up_without_probe`, `test_poison_junk_uid_does_not_block_later_user_move_learn`.

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
