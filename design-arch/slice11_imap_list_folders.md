# Slice 11 — IMAP list folders

Architecture and implementation spec. Execute this document; do not
expand into Slice 12.

**Status:** ready to implement
**Parent plan:** [`allow_block_sliced_plan.md`](allow_block_sliced_plan.md)
(Slice 11 of 9–12)
**Depends on:** Slice 10 (schema, parser, `Db` list helpers,
`actual_name`, caps)
**Primary file:** [`filter/filter.py`](../filter/filter.py)
(`build_folder_map`, `ensure_folders`, `_run_account`, new drain)
**Tests:** new [`filter/test_list_folders.py`](../filter/test_list_folders.py)
(FakeIMAP; same style as
[`filter/test_shadow_mode.py`](../filter/test_shadow_mode.py))
**Docs:** README folder list + hybrid shadow contract; example YAML
folder keys if overridable.

This is the Outlook/OWA gesture: drag mail into Inbox subfolders.
It writes **person** lists only (From address). Domain-wide patterns
stay dashboard/YAML-roster (slice 12 / slice 10).

---

## 1. Goal

1. Auto-create `INBOX/Allowlist` and `INBOX/Blocklist` (English,
   delimiter-aware) on connect, including in `shadow`.
2. Drain those folders each account loop: upsert or flip a
   person-list address from **From only**, then MOVE the message
   **back to Inbox** so mail is not lost.
3. Same-loop `scan_inbox` can then apply a block hit (new Inbox UID
   above the bookmark).

---

## 2. Non-goals

- Dashboard UI (slice 12).
- Writing `pattern_type=domain` from IMAP.
- Matching Sender/Reply-To when **creating** the row (scan still
  uses all three; the stored row is From).
- SPECIAL-USE flags for list folders.
- Localized folder names / YAML override in v1 (defaults only;
  optional override keys may be added if cheap and tested — default
  **no** extra YAML keys unless needed to match `build_folder_map`
  style; see §3).

---

## 3. Folder names (locked)

Under IMAP Inbox, **not** under Junk (Train-* stay under Junk).

Add to `BUILTIN_DEFAULTS` and `Account`:

```
allowlist: INBOX/Allowlist
blocklist: INBOX/Blocklist
```

[`build_folder_map`](../filter/filter.py) (~L1464) gains:

```
"allowlist": resolve_folder(acc.allowlist, delim),
"blocklist": resolve_folder(acc.blocklist, delim),
```

`resolve_folder` already rewrites `/` to the server delimiter
(`INBOX.Allowlist` on some servers). Inbox display names in Outlook
may be translated; IMAP name stays `INBOX`.

[`ensure_folders`](../filter/filter.py) (~L1491):

- `REQUIRED` unchanged: `inbox`, `junk`, `trash` (never auto-create).
- `AUTO_CREATE` add `allowlist`, `blocklist` next to the four
  Train-* keys.
- Shadow **may** CREATE+SUBSCRIBE these (hybrid exception, same
  rationale as Train-*: filter-owned). Document next to slice 1’s
  hybrid table in README.

Do **not** remap list folders when SPECIAL-USE moves Junk (they are
not Junk children).

---

## 4. Drain (locked)

New `_drain_list_folder(client, db, log, acc, fmap, *, kind)` with
`kind in {"allow", "block"}`. Model on `_drain_train_folder`
(~L2558) but:

- **No** `rspamd_learn`.
- Destination is always `fmap["inbox"]`, not Trained-*.
- SELECT the list folder **writable** (`readonly=False`). Shadow
  Inbox may remain EXAMINE; IMAP MOVE is issued while selected on
  the **source**.

Algorithm:

1. `select_with_uidvalidity_check` on `fmap["allowlist"]` or
   `blocklist`. On failure: log, return (do not crash the account
   loop).
2. `SEARCH ALL`. Empty → return.
3. Take UIDs in order up to `acc.max_list_per_run` (default 100).
   Remaining UIDs wait for the next loop.
4. `fetch_under_cap` for bodies.
5. For each UID:
   - Oversize: log `skipped_oversize` (existing helper); still
     include in `to_move` (mail must leave the list folder).
   - Empty body: same — MOVE back, no upsert.
   - `msgid, subject, sender = parse_envelope(raw)` — **From only**.
   - If `sender` empty: log; MOVE back; no upsert.
   - Else parse as a person-list address (`allow_domain=False`).
     If the From value fails the address parser: log; MOVE back;
     no upsert.
   - `list_count` for `(person, acc.actual_name, kind)`. If this
     pattern is **new** and count ≥ `acc.max_list_entries`: log
     `list_cap`; MOVE back; do not insert.
   - Else `list_flip_address` or `list_upsert_address`:
     - If the pattern exists on the **other** kind for this
       person → flip (delete sibling, insert this kind). Event
       `list_flip`. `source=imap`, `actor=acc.name`,
       `sample_message_id=msgid`.
     - Else upsert this kind. Event `list_imap_add`.
   - Never write `pattern_type=domain`.
6. MOVE all handled UIDs (`to_move`) from the list folder to
   `fmap["inbox"]`. Failure: log like train-folder move failure;
   leave UIDs (retry next loop). Do not partial-learn without
   attempting move; prefer: upsert then MOVE; if MOVE fails, the
   row already exists (idempotent upsert) and UID remains for
   retry — acceptable.

`log_event` account = `acc.name`.

### 4.1 Call site

[`_run_account`](../filter/filter.py) (~L2775) inner loop, **after**
`drain_train_spam` / `drain_train_ham`, **before** `scan_inbox`:

```
drain allowlist (kind=allow)
drain blocklist (kind=block)
scan_inbox
execute_due_moves
...
```

Same-loop: MOVE to Inbox assigns a new UID typically above the
scan bookmark → `scan_inbox` can apply a **block** hit immediately.
Allow hits keep the message in Inbox.

---

## 5. Shadow hybrid (locked)

Extend the slice 1 contract in README:

| Action | shadow | flag | move |
|---|---|---|---|
| CREATE `INBOX/Allowlist` + `Blocklist` | yes | yes | yes |
| Drain + MOVE back to Inbox | yes | yes | yes |
| Auto Inbox → Junk from a **scan** block hit | no | flag | yes |

Inbox/Junk/Trash still have no **automatic** spam MOVE in shadow.
Operator-initiated MOVE-back-to-Inbox from a filter-owned child
folder is allowed, like Train-* MOVE.

`inbox_select_readonly` stays True in shadow. List-folder SELECT
is writable.

---

## 6. Tests (locked)

[`filter/test_list_folders.py`](../filter/test_list_folders.py)
with FakeIMAP recording CREATE/MOVE/SELECT.

1. `ensure_folders` in shadow CREATEs allowlist/blocklist, never
   Junk/Trash/Inbox.
2. Drain Allowlist, From `a@x.com`: person allow row; MOVE to Inbox;
   list folder empty of that UID.
3. Same From drained from Blocklist: sibling allow gone, block
   present (`list_flip`).
4. Empty From / oversize: no row (or no new row); still MOVE to
   Inbox.
5. `max_list_per_run=1` with two UIDs: one processed this call.
6. `max_list_entries=1` with a different new From: no insert,
   still MOVE (`list_cap`).
7. From `not a domain pattern` — IMAP must not insert `@x.com`
   even if From were weird; only valid addresses.
8. `_run_account` order is not required to be a full-loop test if
   drain+scan unit tests exist; at least document the call order
   in `_run_account` with a comment.

`cd filter && python -m pytest -q` passes.

---

## 7. Docs

README “Filter-owned folders” / hybrid shadow: list folders.
Operator UX: drag to Allowlist/Blocklist; mail returns to Inbox;
flip by dragging to the other folder; admin removes via dashboard
(slice 12). Copy-vs-move for Train-Ham is **unrelated** (list
folders always MOVE back to Inbox).

---

## 8. Acceptance

- Folders exist on connect (shadow included).
- Drag From-address → person list; message back in Inbox.
- Flip works; IMAP never writes whole-domain patterns.
- Caps enforced without trapping mail in the list folder.
- Scan in the same loop can see the returned Inbox UID.
