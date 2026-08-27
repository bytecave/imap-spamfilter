# Slice 3 — Reliable Inbox bookmark

Architecture and implementation spec. Execute this document; do not
expand into Slices 4–8.

**Status:** implemented
**Parent plan:** sliced code-review fixes (Slice 3 of 8)
**Depends on:** Slice 1 (hybrid shadow), Slice 2 (oversize skip is
terminal). Do not regress those gates.
**Primary file:** [`filter/filter.py`](../filter/filter.py)
**Tests:** new [`filter/test_inbox_bookmark.py`](../filter/test_inbox_bookmark.py).
Reuse `CapIMAP` / helpers from
[`filter/test_fetch_discipline.py`](../filter/test_fetch_discipline.py)
and [`filter/test_shadow_mode.py`](../filter/test_shadow_mode.py). Do not
replace those modules.
**Docs:** README safe-mode note that UNSEEN is counted *above the Inbox
bookmark*, not against the whole folder.

Related review write-up:
[`spamfilter_discussion_and_code_review.md`](spamfilter_discussion_and_code_review.md)
(Inbox UID bookmark advances past failed UIDs).

---

## 1. Goal

A temporary rspamd outage (or a missing body, or SIGTERM mid-batch)
must not permanently skip Inbox mail.

After this slice, `scan_inbox` advances `scan_bookmark` only through a
**prefix of terminally handled UIDs**. The first non-terminal UID stops
both bookmark advance *and* the rest of this pass. The next pass
retries `UID {bookmark+1}:*`. Already-scored UIDs in that range remain
idempotent skips.

No new table. Inbox identity stays UID-based (Slice 5 will change that).

---

## 2. Non-goals

| Later slice | Do not touch now |
|---|---|
| 4 | `tls_mode` / STARTTLS / YAML bools / IDLE fallback |
| 5 | Message-ID primary key |
| 6–8 | rspamd headers, ops, dashboard |

Also out of scope:

- Changing Junk `scan_bookmark` (Slice 2). Junk has no rspamd scan in
  the poll path; leave `set_scan_bookmark(junk, max(uids))` as-is.
- Redesigning safe-mode to use total Inbox UNSEEN. Document the current
  “UNSEEN above the bookmark” trigger only.
- Partial-MIME retry for oversized messages (still terminal from Slice 2).

---

## 3. Why the current code is wrong

After the candidate loop, `scan_inbox` does:

```
new_max = max(candidates)
db.set_scan_bookmark(inbox, uv, new_max)
```

`continue` on `rspamd_scan is None`, missing body, or other failures
still leaves that UID in `candidates`, so the bookmark jumps past it.
A 30-second rspamd blip can leave spam in Inbox forever.

`SHUTDOWN` `return`s inside the loop and *does* skip the advance today.
Keep that. Do not “fix” shutdown by advancing a partial prefix from a
half-written pass if we already returned — returning without a write is
correct.

---

## 4. Terminal vs non-terminal (locked)

Walk `sorted(candidates)`. UIDs are not contiguous; “prefix” means this
ordered list, not `range(bookmark+1, max)`.

**Terminal (advance past this UID):**

| Outcome | Notes |
|---|---|
| Scored successfully (`our_score` written), including below-threshold | Action (shadow/flag/pending_move) is separate |
| Already had `our_score` | Idempotent skip |
| Seen and not scoring (`uid not in unseen`) | First-appearance rule; not a failure |
| Junk→Inbox revert handled | Learn/pending_ham scheduled or skipped |
| No Message-ID | Permanent skip. Log event `no_message_id` (today is debug-only) |
| Oversized skip (Slice 2) | `skipped_oversize`; never auto-move |

**Non-terminal (stop this pass; do not advance past):**

| Outcome | Notes |
|---|---|
| Missing body after a successful under-cap FETCH | Retry next pass |
| `rspamd_scan` returned `None` | Retry next pass |
| `SHUTDOWN` mid-batch | `return` immediately; bookmark unchanged |

Equality: the first non-terminal UID is **not** included in the
bookmark. Bookmark becomes the last terminal UID before it, or is left
unchanged if the prefix is empty.

**Stop vs continue:** this pass **stops** at the first non-terminal UID
(does not score later UIDs). A rspamd outage must not emit dozens of
`scan_failed` events for mail that will be retried together next loop.
Later UIDs are picked up on the next `UID {bookmark+1}:*` pass.

---

## 5. Implementation

Stay inside `scan_inbox`. Track `last_terminal: int | None = None`.
On each terminal outcome set `last_terminal = uid`. On non-terminal,
break out of the chunk loops. After the loops (not on SHUTDOWN
`return`):

```python
if last_terminal is not None:
    with db.tx():
        db.set_scan_bookmark(fmap["inbox"], uv, last_terminal)
```

Delete `new_max = max(candidates)`.

Empty `candidates`: return without writing (bookmark already correct).

Safe-mode enter (`UNSEEN > cap`) still returns before the loop — no
advance. Unchanged.

Add `db.log_event("no_message_id", detail=f"uid={uid}")` on the
permanent no-Message-ID skip so operators can grep it; keep the debug
log line.

---

## 6. Tests

**[`filter/test_inbox_bookmark.py`](../filter/test_inbox_bookmark.py)**

Required:

1. **ok / rspamd-down / ok:** UIDs 1,2,3 all under cap with bodies.
   Monkeypatch `rspamd_scan` to return `1.0` for UID 1, `None` for UID 2,
   `1.0` for UID 3. After one `scan_inbox`: bookmark **1**, UID 1 scored,
   UID 2 `scan_failed`, UID 3 **not** scored (pass stopped).
2. **Retry after recovery:** same fixture, `rspamd_scan` now always
   `1.0`. Second `scan_inbox`: UID 2 and 3 scored, bookmark **3**.
3. **Oversize is terminal:** UID 1 scored, UID 2 oversize. Bookmark **2**
   (Slice 2 skip does not block later mail).
4. **Missing body is non-terminal:** UID 1 scored, UID 2 has SIZE under
   cap but no BODY. Bookmark **1**, not 2.

Do not require live IMAP or rspamd.

---

## 7. Acceptance criteria

1. `cd filter && python -m pytest -q` passes, including new tests and
   Slices 1–2.
2. Three-UID ok/fail/ok leaves bookmark at the first success.
3. After rspamd recovers, the failed UID is scored and the bookmark
   advances through the rest of the prefix.
4. Oversized skip still advances the bookmark (Slice 2).
5. README states that `safe_mode_unseen_cap` counts UNSEEN **above the
   Inbox bookmark**.

---

## 8. Suggested edit order

1. Replace `max(candidates)` advance with terminal-prefix tracking.
2. `no_message_id` event.
3. `test_inbox_bookmark.py`.
4. README safe-mode sentence.
5. Grep `set_scan_bookmark` — Inbox uses prefix; Junk init/advance
   unchanged.
