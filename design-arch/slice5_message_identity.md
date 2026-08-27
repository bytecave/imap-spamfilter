# Slice 5 — Message identity

Architecture and implementation spec. Execute this document; do not
expand into Slices 6–8.

**Status:** implemented
**Parent plan:** sliced code-review fixes (Slice 5 of 8)
**Depends on:** Slice 3 (Inbox bookmark stays UID-based).
**Primary file:** [`filter/filter.py`](../filter/filter.py)
**Tests:** new [`filter/test_message_identity.py`](../filter/test_message_identity.py);
update Db call sites in existing tests. Dashboard SQL stays valid
(message_id is still a column).
**Docs:** one README sentence that Message-ID is metadata, not identity.

Related review: sender-controlled Message-ID as PRIMARY KEY.

---

## 1. Goal

An IMAP message is identified by `(account, folder, uidvalidity, uid)`,
not by `Message-ID`. Two UIDs that share a Message-ID score independently
and must not inherit each other's Junk/Inbox history.

Message-ID remains searchable metadata (dashboard, events). A SHA-256 of
the fetched body correlates a MOVE across folders for user-training
detection only.

---

## 2. Non-goals

Slices 6–8. Partial-MIME hashing of oversized messages (no body ⇒ no
fingerprint; fail closed, no false ham/spam learn). COPYUID / UIDPLUS
destination UIDs after MOVE (poll_junk creates the Junk object).

---

## 3. Schema (locked)

```
messages (
  account, folder, uidvalidity, uid,   -- PK
  message_id,                          -- nullable metadata
  body_sha256,                         -- hex SHA-256 of BODY, nullable
  first_seen, last_seen, current_folder,
  moved_to_junk_at, our_score, our_action,
  learned_as, learned_at, pending_learn, pending_learn_at,
  sender, subject, received_at
)
INDEX (account, message_id)      -- NOT unique
INDEX (account, body_sha256)     -- NOT unique
```

`current_folder` stays denormalized and should equal `folder` for live
objects. After we MOVE Inbox→Junk we keep the Inbox row (stale UID) with
`our_action=moved_to_junk` and `current_folder=junk` so poll_junk can
see “filter put this content here” via `body_sha256` before the new Junk
UID row exists.

**Migration:** if `uid` is missing, copy into a new table using
`folder=current_folder`, `uidvalidity=0`, `uid=old rowid`. Existing PoC
DBs will not match live IMAP objects; operators re-shadow. Fresh installs
use the new SCHEMA directly. `_migrate` also `ADD COLUMN body_sha256`
when the new PK already exists without it.

---

## 4. Db API (locked)

Replace msgid-keyed `get_message` / `upsert_message` / `update_message`:

- `get_imap_message(folder, uidvalidity, uid)`
- `upsert_imap_message(folder, uidvalidity, uid, *, message_id, sender, subject, received_at, body_sha256)`
- `update_imap_message(folder, uidvalidity, uid, **fields)`
- `find_by_sha256(sha256) -> list[Row]` (same account)

Events still store `message_id` when known.

`try_learn(..., folder, uidvalidity, uid)` updates **that IMAP row**.

---

## 5. Call-site behaviour

**scan_inbox.** Identity is `(inbox, uv, uid)`. `prior` is that row.
Revert iff a *different* row with the same `body_sha256` has
`current_folder == junk` or `our_action == moved_to_junk`. Scoring skip
uses `prior.our_score` on **this** UID only. No Message-ID still skips
insert (unchanged) and is terminal.

**poll_junk.** Same: user Inbox→Junk if a sha256 sibling has
`folder/current_folder == inbox` and `our_action != moved_to_junk`.
Filter-initiated MOVE: sibling `our_action == moved_to_junk` → no learn.

**process_pending_learns.** SELECT includes `folder, uidvalidity, uid`.
FETCH that UID. No `SEARCH HEADER Message-ID`. Missing UID →
`pending_lost`.

**execute_due_moves.** `update_imap_message(inbox, uv, uid, current_folder=junk, our_action=moved_to_junk, ...)`.

**_drain_train_folder.** Upsert by train-folder IMAP key. `message_id`
may be NULL. MOVE still by UID list.

**retention.** Update rows by `(src, uv, uid)` after MOVE to Trash, not
`message_id IN (...)`.

---

## 6. Tests

1. Two Inbox UIDs, identical Message-ID, different bodies: both scored;
   `our_score` on each IMAP row; no shared skip.
2. Junk sibling with Message-ID X / sha256(A); Inbox UID with Message-ID
   X / sha256(B): Inbox is **not** treated as a ham revert.
3. `process_pending_learns` FETCHes the stored UID even if HEADER search
   would return a different UID (FakeIMAP HEADER returns the wrong one).

Existing tests updated to IMAP keys. `_mk_db` still applies SCHEMA
(new) + `_migrate`.

---

## 7. Acceptance

`cd filter && python -m pytest -q` passes. No HEADER Message-ID search
in `process_pending_learns`. README: identity is IMAP UID, not Message-ID.
