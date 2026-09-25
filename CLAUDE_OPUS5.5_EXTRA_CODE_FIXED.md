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
