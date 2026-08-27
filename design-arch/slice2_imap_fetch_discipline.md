# Slice 2 — IMAP fetch discipline

Architecture and implementation spec. Execute this document; do not
expand into Slices 3–8.

**Status:** implemented
**Parent plan:** sliced code-review fixes (Slice 2 of 8)
**Depends on:** Slice 1 (hybrid shadow). Do not regress those SELECT/MOVE gates.
**Primary file:** [`filter/filter.py`](../filter/filter.py)
**Tests:** new [`filter/test_fetch_discipline.py`](../filter/test_fetch_discipline.py);
update FakeIMAP in [`filter/test_shadow_mode.py`](../filter/test_shadow_mode.py)
and [`filter/test_learn.py`](../filter/test_learn.py) so two-phase FETCH
still works. Do not replace those modules.
**Docs:** one short README note that messages over 5 MiB are never
auto-moved / never fully downloaded.

Related review write-up:
[`spamfilter_discussion_and_code_review.md`](spamfilter_discussion_and_code_review.md)
(unenforced `MAX_FETCH_BYTES`; full Junk re-download every poll).

---

## 1. Goal

Stop the filter from pulling entire MIME payloads (attachments included)
into RAM, and stop re-downloading the whole Junk folder every
`junk_poll_interval`.

After this slice:

- `BODY.PEEK[]` is issued only when `RFC822.SIZE` is known and
  `<= MAX_FETCH_BYTES` (5 MiB, already defined, still not configurable).
- Oversized mail stays where it is, is never scored/moved/flagged, and
  is logged as `skipped_oversize`. Fail closed.
- `poll_junk` uses the existing `scan_bookmark` table (folder is already
  in the PK) the same way Inbox does: first pass records max UID and
  does not process history; later passes `SEARCH UID {bookmark+1}:*`.

---

## 2. Non-goals

| Later slice | Do not touch now |
|---|---|
| 3 | Inbox bookmark advance-past-failure (oversize skip is terminal for Slice 3; do not change Inbox bookmark algorithm here) |
| 4 | `tls_mode` / STARTTLS / YAML bools / IDLE fallback |
| 5 | Message-ID primary key |
| 6 | rspamd `From`/`Rcpt` headers |
| 7–8 | ops, dashboard |

Also out of scope:

- Making `MAX_FETCH_BYTES` per-account configurable.
- Partial-MIME scanning of oversized messages (headers-only classify).
  v1 skips them entirely.
- [`filter/bootstrap_train.py`](../filter/bootstrap_train.py) full-folder
  FETCH. That is a one-shot operator CLI, not the 15-account 120s loop.
- Changing `SCAN_FETCH_CHUNK`.
- Rewriting `fetch_chunked` callers that do not request full bodies
  (`execute_due_moves` FLAGS, retention HEADER.FIELDS).

---

## 3. Why the current code is wrong

`MAX_FETCH_BYTES = 5 * 1024 * 1024` is defined and never consulted.
`scan_inbox`, `poll_junk`, `_drain_train_folder`, and
`process_pending_learns` all `FETCH BODY.PEEK[]` first.

`poll_junk` does `SEARCH ALL` then body-FETCH every 120s. A 2,000-message
Junk folder is re-downloaded on every pass. `scan_bookmark` already keys
`(account, folder, uidvalidity)` — Inbox uses it; Junk does not.

---

## 4. Policy (locked)

| Situation | IMAP | Scoring / learn / MOVE |
|---|---|---|
| `RFC822.SIZE` known and `<= 5 MiB` | FETCH `BODY.PEEK[]` | existing behaviour |
| `RFC822.SIZE` known and `> 5 MiB` | no body FETCH | skip; log `skipped_oversize`; never auto-move / flag / score |
| `RFC822.SIZE` missing / unparseable | no body FETCH | same as oversize (fail closed) |
| Inbox oversize | stay in Inbox | bookmark still advances with the batch (Slice 3 will call this terminal) |
| Junk oversize | stay in Junk | junk bookmark still advances; no Bayes learn |
| Train-* oversize | **MOVE to Trained-*** unlearned | so `SEARCH ALL` drain cannot loop on the same UID |
| Pending-learn oversize | stay in folder | clear `pending_learn` so we do not retry a 20 MiB FETCH every poll |

Equality: `size == MAX_FETCH_BYTES` is allowed (fetch body).

---

## 5. Architecture

One helper, used at every full-body FETCH site. Do not copy the
two-phase logic into each loop.

```python
def _rfc822_size(data: dict) -> int | None:
    ...


def fetch_under_cap(
    client: IMAPClient, uids: list[int]
) -> Iterator[tuple[int, dict, bool]]:
    """Yield (uid, merged_data, oversize) in SCAN_FETCH_CHUNK batches.

    Phase 1: FETCH RFC822.SIZE, FLAGS, INTERNALDATE.
    Phase 2: FETCH BODY.PEEK[] only for uids whose SIZE is known and
    <= MAX_FETCH_BYTES. oversize=True ⇒ no body in data.
    """
```

Place next to `fetch_chunked`. Keep `fetch_chunked` for non-body FETCHes.

A tiny `_log_skipped_oversize(db, log, *, uid, folder, size, msgid=None)`
avoids four slightly different log lines. Event name is exactly
`skipped_oversize` (dashboard/SQLite grep).

### 5.1 Call sites

| Site | Change |
|---|---|
| `scan_inbox` | replace `client.fetch(..., BODY.PEEK[])` with `fetch_under_cap`; on oversize log and `continue` |
| `poll_junk` | Junk watermark (§5.2) + `fetch_under_cap` for new UIDs |
| `_drain_train_folder` | `fetch_under_cap` instead of `fetch_chunked(..., BODY.PEEK[])`; oversize UIDs go on the MOVE-to-Trained list without `try_learn` |
| `process_pending_learns` | `fetch_under_cap([uid])`; oversize clears pending_learn + event |

### 5.2 Junk watermark

Mirror Inbox init, keyed on `fmap["junk"]` + uidvalidity from
`select_with_uidvalidity_check` (today the return value is discarded).

```
uv = select_with_uidvalidity_check(..., junk, readonly=True)
bookmark = db.get_scan_bookmark(junk, uv)
if bookmark is None:
    existing = SEARCH ALL
    set_scan_bookmark(junk, uv, max or 0)
    log + event scan_bookmark_init (folder name in detail so it is
    distinguishable from Inbox init)
    process_pending_learns(...)
    return

uids = SEARCH UID {bookmark+1}:*
uids = sorted(u for u in uids if u > bookmark)  # some servers return `*`
# fetch_under_cap + existing Inbox→Junk learn logic
if uids:
    set_scan_bookmark(junk, uv, max(uids))
process_pending_learns(...)
```

Empty Junk on first connect: bookmark 0, then pending learns. Historical
Junk is never body-FETCHed.

User Inbox→Junk MOVEs get a new destination UID above the watermark, so
training still works.

`process_pending_learns` stays DB-driven. It must not become a full Junk
rescan.

UIDVALIDITY change already deletes `scan_bookmark` for that folder in
`select_with_uidvalidity_check`. No schema change.

---

## 6. Tests

**[`filter/test_fetch_discipline.py`](../filter/test_fetch_discipline.py)**

FakeIMAP records `(uids, parts)` per `fetch` and **raises** if any
requested part is `BODY[]` / `BODY.PEEK[]` for a UID whose SIZE exceeds
`MAX_FETCH_BYTES`.

Required cases:

1. **Cap helper / scan_inbox:** one UID under cap, one over. Body FETCH
   only for the small UID. Large UID: `skipped_oversize` event, no
   `our_score`, no flags, no `pending_move`. Monkeypatch `rspamd_scan`.
   Pre-seed Inbox `scan_bookmark` like Slice 1 tests.
2. **poll_junk init:** folder has UIDs 1..3, no bookmark. First call
   issues no `BODY.PEEK[]`, sets bookmark to 3.
3. **poll_junk incremental:** bookmark=3, folder has 1,2,3,4. Second
   call may BODY-FETCH only UID 4 (SIZE fetch of 4 is fine). After the
   call bookmark is 4.
4. **drain oversize:** Train-Spam UID with SIZE > cap; `rspamd_learn`
   must not be called; `move` **is** called to Trained-Spam; no
   `BODY.PEEK[]` for that UID.

Update `RecordingIMAP.fetch` / `_FakeIMAP.fetch` to answer `RFC822.SIZE`
(default `len(body)`) and to ignore unrequested parts, so Slice 1 and
learn tests keep passing.

---

## 7. Acceptance criteria

1. `cd filter && python -m pytest -q` passes.
2. No `BODY.PEEK[]` / `BODY[]` FETCH for a UID with SIZE > 5 MiB
   (enforced by the FakeIMAP assertion in tests).
3. `poll_junk` does not body-FETCH historical Junk after bookmark init.
4. Train-* oversize is moved to Trained-* without learning.
5. Slice 1 shadow/flag/drain tests still pass.
6. README states the 5 MiB skip.

---

## 8. Suggested edit order

1. `_rfc822_size`, `fetch_under_cap`, `_log_skipped_oversize`.
2. `scan_inbox` loop.
3. `poll_junk` watermark + loop.
4. `_drain_train_folder` + `process_pending_learns`.
5. Tests + FakeIMAP SIZE handling.
6. README one-liner.
7. Grep remaining `BODY.PEEK[]` full-body FETCHes in `filter.py`.
