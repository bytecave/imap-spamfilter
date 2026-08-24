# Slice 1 — Hybrid shadow mode

Architecture and implementation spec. Execute this document; do not
expand into Slices 2–8.

**Status:** ready to implement
**Parent plan:** sliced code-review fixes (Slice 1 of 8)
**Policy choice:** hybrid (Inbox/Junk/Trash never mutated in `shadow`;
Train-* create + drain remain allowed)
**Primary file:** [`filter/filter.py`](../filter/filter.py)
**Tests:** [`filter/test_learn.py`](../filter/test_learn.py) (extend; do
not replace the existing learn-path tests)
**Docs:** [`README.md`](../README.md), [`accounts.yml.example`](../accounts.yml.example)

Related review write-up:
[`spamfilter_discussion_and_code_review.md`](spamfilter_discussion_and_code_review.md)
(the “shadow mode isn’t actually read-only” finding).

---

## 1. Goal

Make `mode: shadow` match the product contract an operator relies on
during first-week evaluation:

- Score Inbox mail and log what *would* have happened.
- Never MOVE, STORE, or otherwise mutate **Inbox, Junk, or Trash**.
- Still allow the operator to bootstrap Bayes by dropping mail into the
  filter-owned **Train-*** folders.

After this slice, connecting a production mailbox in `shadow` cannot
age Junk into Trash, cannot flag Inbox messages, and cannot MOVE Inbox
to Junk. It *can* create `Junk/Train-*` / `Junk/Trained-*` and MOVE
within that subtree after a learn.

---

## 2. Non-goals

Leave these for later slices. Do not “while we’re here” them.

| Later slice | Do not touch now |
|---|---|
| 2 | `MAX_FETCH_BYTES`, Junk UID watermark, `RFC822.SIZE` |
| 3 | Inbox bookmark advance-past-failure |
| 4 | `tls_mode` / STARTTLS / YAML bools / IDLE fallback |
| 5 | Message-ID primary key |
| 6 | rspamd `From`/`Rcpt` headers |
| 7 | bootstrap.sh permissions, image pins, env-file path |
| 8 | dashboard hardening |

Also out of scope here:

- Changing `junk_retention_days` default (10). Shadow simply will not
  run retention, so the default cannot bite until promotion to
  `flag`/`move`.
- Disabling Bayes learns from *observed* Inbox↔Junk user moves. Those
  are HTTP POSTs to rspamd, not IMAP writes. Hybrid policy keeps them.
- Rewriting `account_loop` / reconnect / IDLE chunking, except the
  Inbox SELECT `readonly` flag in shadow (see §6).

---

## 3. Why the current code is wrong

`acc.mode` is consulted in **two** places only:

1. `scan_inbox` (~1301) — `match acc.mode` chooses log vs `\Flagged` vs
   `pending_move`.
2. `execute_due_moves` (~1336) — `if acc.mode != "move": return`.

Everything else in `_run_account` (~1842–1864) runs in every mode:

```
ensure_folders          CREATE+SUBSCRIBE Train-*
drain_train_spam/ham    rspamd learn + MOVE Train-* → Trained-*
scan_inbox              SELECT Inbox (writable), FETCH, maybe STORE
execute_due_moves       MOVE Inbox → Junk          (already gated)
poll_junk               SELECT Junk (writable), FETCH, schedule learns
retention_sweep         MOVE Junk/Trained-* → Trash
select_folder(Inbox)    writable SELECT before IDLE
```

Built-in defaults `junk_retention_days=10` and
`trained_retention_days=7` therefore MOVE mail to Trash on a fresh
`shadow` install as soon as the first hourly retention pass sees
messages older than the cutoff.

README today says `shadow - scan + log only, no mailbox writes`. That
sentence is false. This slice makes it true for Inbox/Junk/Trash, and
narrows the remaining writes to the Train-* subtree the filter owns.

---

## 4. Write policy (locked)

| Action | IMAP effect | shadow | flag | move |
|---|---|---|---|---|
| FETCH / SEARCH / LIST / IDLE | none (BODY.PEEK) | yes | yes | yes |
| CREATE+SUBSCRIBE Train-* / Trained-* | CREATE, SUBSCRIBE | **yes** | yes | yes |
| Drain Train-* (learn + MOVE to Trained-*) | MOVE | **yes** | yes | yes |
| STORE `\Flagged` on Inbox | STORE | **no** | yes | no |
| MOVE Inbox → Junk | MOVE | **no** | no | yes |
| Retention MOVE Junk / Trained-* → Trash | MOVE | **no** | yes | yes |
| Bayes learn from observed Inbox↔Junk moves | HTTP only | yes | yes | yes |
| Local SQLite (events, scores, pending_learn) | none | yes | yes | yes |

Notes:

- `move` does **not** set `\Flagged`. That is existing behaviour
  (comment at ~1313) and stays.
- CREATE of Inbox/Junk/Trash remains forbidden in every mode
  (`ensure_folders` REQUIRED vs AUTO_CREATE). Unchanged.
- `learn_from_moves: false` still disables all Bayes POSTs, including
  Train-* drain. Orthogonal to mode.

---

## 5. Architecture

One small policy helper, applied at every IMAP-mutating call site.
Do not scatter `if acc.mode == "shadow"` across the file.

```python
def inbox_select_readonly(acc: Account) -> bool:
    """EXAMINE rather than SELECT. Shadow must not be able to set flags
    even if a future FETCH drops PEEK. Flag mode needs a writable
    SELECT for STORE \\Flagged. Move mode's MOVEs happen in
    execute_due_moves, which SELECTs Inbox itself."""
    return acc.mode == "shadow"


def mode_allows_retention(acc: Account) -> bool:
    """Junk / Trained-* → Trash. Blocked in shadow so a 10-day default
    cannot empty Junk during evaluation."""
    return acc.mode in ("flag", "move")
```

That is the entire policy surface. Flag vs move for Inbox mutations is
already encoded in `scan_inbox`’s `match` and `execute_due_moves`’s
early return. Do not reimplement those.

### 5.1 SELECT vs EXAMINE

`select_with_uidvalidity_check` already takes `readonly: bool = False`
and passes it to `client.select_folder`. `readonly=True` is IMAP
EXAMINE: the mailbox is selected, IDLE still works (RFC 2177), STORE
and MOVE are rejected by the server.

| Call site | Today | After Slice 1 |
|---|---|---|
| `ensure_folders` existence probe | `readonly=True` | unchanged |
| `scan_inbox` | writable | `readonly=inbox_select_readonly(acc)` |
| `execute_due_moves` | writable | unchanged (move-only; already returns in shadow) |
| `poll_junk` | writable | **always `readonly=True`** — this function never STORE/MOVE |
| `process_pending_learns` | writable | **always `readonly=True`** — FETCH only |
| `_drain_train_folder` | writable | unchanged (needs MOVE) |
| `_sweep_folder_to_trash` | writable | unchanged, but function is not entered in shadow |
| IDLE setup `_run_account` ~1899 | writable `select_folder` | `readonly=inbox_select_readonly(acc)` |

`poll_junk` becoming always-readonly is a behaviour-preserving
tightening in flag/move as well: retention does its own SELECT later.
If a server is broken and EXAMINE-then-SELECT on the same folder in one
connection is problematic, keep poll_junk writable in flag/move and
only force readonly in shadow. Prefer always-readonly first; the tests
in §9 will pin whichever we ship.

### 5.2 Retention vs local DB prune

In `_run_account` the hourly block is:

```
if now - state.last_retention >= acc.retention_check_interval:
    retention_sweep(...)
    prune_stale_pending_learn / prune_events / prune_messages
    vacuum_if_due()
    state.last_retention = now
```

`retention_sweep` must become a no-op in shadow. **The prune/vacuum
must still run.** Do not wrap the whole `if` body in a mode check.
Implement the gate inside `retention_sweep` (or `_sweep_folder_to_trash`)
so the caller stays unchanged.

### 5.3 Logging

When `retention_sweep` bails because of mode, log **once per call** at
INFO (the function already runs at most once per
`retention_check_interval`):

```
retention_sweep skipped (mode=shadow; Junk/Trained-* not moved to Trash)
```

Do not add a new SQLite event type unless tests need it; a log line is
enough. Operators watching `docker logs` during week-one shadow eval
need to see that retention is intentionally off.

`scan_inbox` already logs `[shadow] would flag ...`. Keep that.

No new log when Train-* CREATE/drain happens in shadow — that is
wanted behaviour, not a surprise.

---

## 6. Call-site implementation

Work in [`filter/filter.py`](../filter/filter.py). Keep helpers next to
`VALID_MODES` / `Account` (config section) or just above `ensure_folders`.

### 6.1 `retention_sweep` (~1660)

First lines become:

```python
def retention_sweep(...):
    if not mode_allows_retention(acc):
        log.info(
            "retention_sweep skipped (mode=%s; Junk/Trained-* not moved to Trash)",
            acc.mode,
        )
        return
    # existing junk_retention_days / trained_retention_days body
```

`_sweep_folder_to_trash` already no-ops on `in_safe_mode("all")`. Leave
that. Mode is a separate axis.

### 6.2 `scan_inbox` (~1162)

```python
uv = select_with_uidvalidity_check(
    client, db, fmap["inbox"], log, readonly=inbox_select_readonly(acc)
)
```

The `match acc.mode` block (~1301–1321) already does the right thing
for STORE / pending_move. No change required there. Shadow cannot reach
`add_flags` or `add_pending_move`.

Do **not** skip scoring in shadow. Scoring + `our_action="shadow"` is
the whole point of the evaluation week.

### 6.3 `poll_junk` (~1394)

```python
select_with_uidvalidity_check(
    client, db, fmap["junk"], log, readonly=True
)
```

No other change. Observed Inbox→Junk still schedules `pending_learn`
and `try_learn`. Those are Bayes, not mailbox writes.

### 6.4 `process_pending_learns` (~1475)

```python
client.select_folder(folder, readonly=True)
```

FETCH BODY.PEEK does not need a writable select. If IMAPClient’s
`select_folder` signature uses the `readonly=` kwarg (it does; already
used in `ensure_folders`), pass it.

### 6.5 `_run_account` IDLE SELECT (~1899)

Today:

```python
client.select_folder(acc.folder_map["inbox"])
```

Change to:

```python
client.select_folder(
    acc.folder_map["inbox"],
    readonly=inbox_select_readonly(acc),
)
```

Do not change IDLE chunking, `idle_timeout`, or the no-IDLE fallback
(Slice 4).

### 6.6 `ensure_folders` (~870)

No code change for mode. AUTO_CREATE of Train-* stays on in shadow
(hybrid). REQUIRED folders stay probe-only.

Add a one-line comment above AUTO_CREATE stating that shadow is
allowed to create these because they are filter-owned.

### 6.7 `_drain_train_folder` / `drain_train_*`

No mode gate. Hybrid explicitly keeps this path in shadow.

### 6.8 `execute_due_moves`

Already `if acc.mode != "move": return`. Add nothing.

---

## 7. Documentation

### 7.1 README

Architecture blurb (~21) currently:

```
shadow  - scan + log only, no mailbox writes
```

Replace with something that cannot be misread:

```
shadow  - scan + log only. No writes to Inbox, Junk, or Trash.
          Train-* folders may still be created and drained so Bayes
          can be bootstrapped during evaluation.
flag    - shadow + sets \Flagged on suspect Inbox mail; retention on
move    - flag + after move_grace_seconds, MOVEs Inbox → Junk
```

Mode-promotion section (~244) should mention: retention (Junk → Trash)
starts when you leave `shadow`. A mailbox that has sat in shadow for
weeks may have old Junk; the first `flag`/`move` retention pass will
honour `junk_retention_days`. If that is too aggressive, set
`junk_retention_days: 0` (or a larger number) **before** promoting.

### 7.2 `accounts.yml.example`

Line 10 today: `shadow - scan + log only, no mailbox writes`. Match the
README wording. Optionally add a commented:

```yaml
# junk_retention_days: 0   # set before promoting out of shadow if you
#                          # do not want aged Junk moved to Trash
```

Do not change the built-in default of 10 in `BUILTIN_DEFAULTS`.

---

## 8. Tests

Add a new test module rather than overloading `test_learn.py`:

**[`filter/test_shadow_mode.py`](../filter/test_shadow_mode.py)**

Keep `test_learn.py` as-is. CI is `cd filter && python -m pytest -q`,
so the new file is picked up automatically.

Reuse `_mk_account` / `_mk_db` from `test_learn.py` **or** extract those
helpers into a tiny `conftest.py` / `testutil.py` if import-from-sibling
is awkward. Prefer a shared `filter/testutil.py` only if the duplication
is more than ~40 lines; otherwise copy `_mk_account` into the new file
for this slice (Slice 4 will have to touch `_mk_account` anyway when
`tls_mode` is added).

### 8.1 FakeIMAP for this slice

Record mutating calls. Minimum surface:

```python
class RecordingIMAP:
    def __init__(self):
        self.created: list[str] = []
        self.subscribed: list[str] = []
        self.moved: list[tuple[list[int], str]] = []  # (uids, dest)
        self.flags_added: list[tuple] = []
        self.selects: list[tuple[str, bool]] = []     # (folder, readonly)

    def select_folder(self, folder, readonly=False, **kw):
        self.selects.append((folder, bool(readonly)))
        return {b"UIDVALIDITY": 1}

    def create_folder(self, name): self.created.append(name)
    def subscribe_folder(self, name): self.subscribed.append(name)
    def move(self, uids, dest): self.moved.append((list(uids), dest))
    def add_flags(self, uid, flags): self.flags_added.append((uid, flags))
    def list_folders(self): return []  # unused if fmap is pre-built
    def search(self, criteria): return []
    def fetch(self, uids, parts): return {}
```

`ensure_folders` probes with `select_folder` and treats
`IMAPClientError` as missing. To exercise CREATE, make the first
select of each Train-* name raise `IMAPClientError`, then succeed
after create — or have `select_folder` raise for names not in an
`existing` set that CREATE adds to.

Use `imapclient.exceptions.IMAPClientError` (already imported in
`filter.py`).

### 8.2 Required cases

**Helpers**

- `inbox_select_readonly` is True only for `mode="shadow"`.
- `mode_allows_retention` is False for shadow, True for flag and move.

**`ensure_folders` in shadow**

- Inbox/Junk/Trash already exist → no CREATE of those names.
- Train-* missing → CREATE+SUBSCRIBE of the four filter-owned folders.
- Never CREATE `INBOX` / `Junk` / `Trash`.

**`retention_sweep` in shadow**

- Even with `junk_retention_days=10` and a FakeIMAP whose `search`
  would return UIDs: `move` is never called.
- INFO log (caplog) mentions `mode=shadow`.

**`retention_sweep` in flag and move**

- With a FakeIMAP that returns some UIDs from `SEARCH BEFORE` and a
  header FETCH: `move` **is** called with dest=`Trash`. (Keep this
  fixture minimal — the point is the mode gate, not retention date
  math. Slice 2/3 own deeper FETCH tests.)

**`scan_inbox` shadow vs flag**

- Shadow, score ≥ threshold: `add_flags` never called, `pending_move`
  table empty, `our_action=="shadow"`.
- Flag, score ≥ threshold: `add_flags` called with `\\Flagged`.
- Shadow Inbox SELECT is `readonly=True`.
- Flag Inbox SELECT is `readonly=False`.

Scoring in these tests needs a stubbed `rspamd_scan`. Monkeypatch
`filter.rspamd_scan` to return `9.0` (above default threshold 8.0).
FakeIMAP `search` must return at least one UID above the bookmark:
initialize the bookmark first (call `scan_inbox` once with empty
search / or `set_scan_bookmark` directly) so the second call is the
one under test. Bookmark init currently `SEARCH ALL` then returns
without processing — tests must either:

1. Pre-seed `scan_bookmark` via `db.set_scan_bookmark("INBOX", uv, 0)`
   so UID 1 is a candidate, or
2. First call init, second call process.

Prefer (1). It avoids depending on bookmark-init behaviour that Slice 3
will change.

**`execute_due_moves` in shadow**

- Insert a due `pending_move` row. Call `execute_due_moves` with
  `mode="shadow"`. `move` is never called. (Already true today; pin it
  so a future refactor cannot drop the gate.)

**`_drain_train_folder` in shadow**

- Train-Spam contains one UID, `rspamd_learn` monkeypatched to
  `"learned"`. `move` **is** called with dest=`Trained-Spam`.
  This is the hybrid contract: Train-* writes remain legal.

Do not require a live IMAP or rspamd.

---

## 9. Acceptance criteria

Slice 1 is done when all of the following hold:

1. `cd filter && python -m pytest -q` passes, including the new file.
2. A `mode=shadow` account with default retention **cannot** MOVE
   anything out of Junk or Trained-* to Trash (test + code gate).
3. A `mode=shadow` account **can** CREATE missing Train-* folders and
   MOVE from Train-* to Trained-* after a successful learn.
4. `scan_inbox` in shadow still writes `our_score` / `our_action="shadow"`
   to SQLite and never STORE-flags or enqueues `pending_move`.
5. Inbox (and Junk poll) SELECT in shadow uses `readonly=True`.
6. Hourly prune/vacuum still runs in shadow (retention skip is inside
   `retention_sweep`, not around the whole hourly block).
7. README and `accounts.yml.example` describe the hybrid contract, and
   warn that promoting out of shadow enables retention.

No Docker or VPS deploy is required to close this slice. Verify with
pytest on the desktop.

---

## 10. Risks and edge cases

**EXAMINE + IDLE on picky servers.** Some IMAP implementations are
sloppy about IDLE in EXAMINE. If a server NAKs IDLE after EXAMINE, the
existing `idle_done` failure path already reconnects. If that proves
noisy on Microsoft 365, fall back to writable SELECT in shadow *but
keep every mutating call gated* — EXAMINE is defense in depth, the
gates are the real fix. Do not pre-emptively keep writable SELECT
“just in case.”

**First promotion to flag/move.** Junk that accumulated for weeks in
shadow becomes eligible for retention on the next hourly pass. Document
it (README). Do not auto-set `junk_retention_days: 0` on promotion.

**Train-* CREATE on a mailbox the operator never wanted touched.**
Hybrid accepts this. The four folders live under the SPECIAL-USE Junk
parent and are the documented bootstrap path. Strict read-only (no
CREATE at all) was rejected.

**`select_folder` without `readonly` elsewhere.** Grep `select_folder(`
and `select_with_uidvalidity_check(` after the change. Every remaining
writable SELECT must be a path that is allowed to MOVE or STORE
(`_drain_train_folder`, `_sweep_folder_to_trash`, `execute_due_moves`,
flag-mode `scan_inbox`).

**Safe-mode interaction.** Unchanged. `in_safe_mode("all")` still
blocks retention and moves independently of `acc.mode`.

---

## 11. Suggested edit order

1. Add `inbox_select_readonly` and `mode_allows_retention`.
2. Gate `retention_sweep`; add the INFO log.
3. Pass `readonly=` at the SELECT call sites in §6.
4. Comment on `ensure_folders` AUTO_CREATE (shadow-allowed).
5. New `test_shadow_mode.py` covering §8.2.
6. README + `accounts.yml.example`.
7. Grep for unguarded `create_folder` / `move` / `add_flags` /
   `select_folder(` and confirm each is classified.

Do not bump image tags or touch `bootstrap.sh` in this slice.
