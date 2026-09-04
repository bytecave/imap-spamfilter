# Slice 10 — List core

Architecture and implementation spec. Execute this document; do not
expand into Slices 11–12.

**Status:** ready to implement
**Parent plan:** [`allow_block_sliced_plan.md`](allow_block_sliced_plan.md)
(Slice 10 of 9–12)
**Depends on:** none (Bayes slice 9 is independent). Slices 11–12
**require** this slice.
**Primary file:** [`filter/filter.py`](../filter/filter.py)
(`Account`, `load_accounts`, `SCHEMA_TABLES`, `Db`, `scan_inbox`)
**Tests:** new [`filter/test_address_lists.py`](../filter/test_address_lists.py)
**Docs:** [`accounts.yml.example`](../accounts.yml.example),
[`README.md`](../README.md) configuration reference; do **not** remove
the README “No allowlist” limitation until slice 11 or 12 actually
ships a complete operator path (note in README that core matching
lands in 10).

This slice is backend only: YAML roster, SQLite rows, shared parser,
header extraction, `scan_inbox` route override. No IMAP list folders,
no dashboard writes.

---

## 1. Goal

1. Operators declare which domains have **domain-scoped** lists in
   YAML (`list_domains`). Dashboard (slice 12) cannot change that
   roster.
2. Every account has a required `actual_name`. Person-scoped lists
   key on that exact string and apply to every account sharing it.
3. Allow/block rows live in SQLite. Matching runs on Inbox scan
   using From + Sender + Reply-To. Hits override flag/move only;
   rspamd still scores; Bayes does not learn from a hit.
4. Tests lock parser, loader, precedence, and scan routing without
   needing Outlook or the dashboard.

---

## 2. Non-goals

- IMAP `INBOX/Allowlist` / `INBOX/Blocklist` (slice 11).
- Dashboard list UI (slice 12).
- Junk-poll un-junk / apply lists outside `scan_inbox`.
- Wildcards, M365 import, auto-allow of the mailbox’s own domain.
- Changing `bayes_user` (slice 9).

---

## 3. YAML (locked)

### 3.1 Root roster — `list_domains`

Today [`ROOT_CONFIG_KEYS`](../filter/filter.py) (~L267) is
`{"defaults", "accounts"}`. Add `list_domains`. It is **root-level**,
not merged onto each `Account` (unlike keys in `defaults:`).

```yaml
list_domains:
  - domain: bytecave.net
    type: company
  - domain: eizenhoefer.net
    type: personal
  - domain: rjmetalfab.com
    type: company
  - domain: bytelord.net
    type: company
```

Loader rules:

- If omitted or `[]`: no domain-scoped lists. Person lists still work.
- Must be a list of mappings. Unknown keys on an entry → `SystemExit`.
- `domain` required, stripped, stored **lowercased**, must be unique
  after lowercasing.
- `type` required, one of `company` | `personal`.
- `type` is **metadata only** in v1 (dashboard grouping). Matching
  does not branch on it.

Return a process-level `ListRoster` from `load_accounts` (change the
return type or add a parallel loader). Implementation choice: either

- `load_accounts(path) -> tuple[list[Account], ListRoster]`, or
- `load_config(path) -> Config` with `.accounts` and `.roster`.

Callers: `main()`, `bootstrap_train.py`, dashboard `_known_accounts()`,
tests. Prefer a small `load_config` wrapper so `bootstrap_train` can
keep using accounts only. **Do not** attach the full roster as a
mutable global without tests; `main()` stores it where `scan_inbox`
can read it (function argument or a frozen object on each `Account`
is fine: `acc.list_roster` as a shared immutable reference).

ByteLord live `accounts.yml` (gitignored) must gain this block when
slice 10 is deployed. Example file documents the four domains.

### 3.2 Per-account `actual_name`

Add to `REQUIRED_PER_ACCOUNT` (~L266) alongside `name`, `user`,
`password`, `imap_host`.

- Strip surrounding whitespace.
- Reject empty after strip.
- Reject CR/LF (same rule as `user` / `name` in `validate_account`
  ~L665).
- **Do not** lowercase. `"Rich Eizenhoefer"` on three accounts is
  one person list. `"rich eizenhoefer"` is a different key.
- Add `actual_name: str` on [`Account`](../filter/filter.py) (~L154).
- Add to `ACCOUNT_CONFIG_KEYS` (via `REQUIRED_PER_ACCOUNT` union).

### 3.3 Caps

Add to `BUILTIN_DEFAULTS`, `Account`, `validate_account` interval
checks (~L707 style):

| Key | Default | Range |
|---|---|---|
| `max_list_per_run` | 100 | 1..5000 |
| `max_list_entries` | 1000 | 1..10000 |

Slice 11 uses `max_list_per_run`. Slice 10 uses `max_list_entries`
when inserting from scan-time… **no** — scan does not insert. Caps
are stored on `Account` in this slice so 11–12 do not redo YAML.
`list_upsert` in this slice must still **enforce** `max_list_entries`
so dashboard/IMAP can call one helper later.

### 3.4 Mailbox domain

Domain-list lookup key = lowercase domain of `acc.user` (IMAP user,
e.g. `steve@rjmetalfab.com` → `rjmetalfab.com`). If that domain is
**not** in the roster, skip domain-list lookups (person list still
applies). `bytelord.net` on the roster with zero accounts is valid
(empty lists until a mailbox exists).

---

## 4. Schema (locked)

Append to `SCHEMA_TABLES` (~L744) and `SCHEMA_INDEXES` (~L825).
`CREATE TABLE IF NOT EXISTS` is enough for upgrade; `_migrate` does
not backfill. Empty table on first boot. No M365 import.

```sql
CREATE TABLE IF NOT EXISTS address_lists (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    scope_type        TEXT NOT NULL,  -- 'person' | 'domain'
    scope_key         TEXT NOT NULL,  -- actual_name or lowercase domain
    kind              TEXT NOT NULL,  -- 'allow' | 'block'
    pattern           TEXT NOT NULL,  -- lowercase 'user@host' or '@host'
    pattern_type      TEXT NOT NULL,  -- 'address' | 'domain'
    source            TEXT NOT NULL,  -- 'imap' | 'dashboard'
    actor             TEXT,
    sample_message_id TEXT,
    created_at        INTEGER NOT NULL,
    updated_at        INTEGER NOT NULL,
    UNIQUE(scope_type, scope_key, kind, pattern)
);

CREATE INDEX IF NOT EXISTS idx_address_lists_lookup
    ON address_lists(scope_type, scope_key, kind, pattern);
```

Check constraints in Python (do not rely on SQLite CHECK). Valid
enums as above. `pattern_type=domain` only with `scope_type=domain`.

### 4.1 `Db` helpers

`log_event` stays per-account (`self.account`). List **hits** during
scan log against the scanning mailbox. List **mutations** log against
the scanning account for IMAP (slice 11) or a dedicated account name
for dashboard (slice 12 may use `account="dashboard"` or the admin
username in `actor` and a sentinel account — pick one and test it;
prefer `account` = scanning IMAP `acc.name` for scan/IMAP events, and
for dashboard-only saves use `account="_dashboard"` plus `actor` =
session user so Events pages stay filterable).

Methods on `Db` (or a small `AddressLists` module using the same
connection):

- `list_get(scope_type, scope_key, kind) -> list[str]` patterns
  sorted.
- `list_count(scope_type, scope_key, kind) -> int`
- `list_upsert_address(...)` — insert or bump `updated_at`; no-op
  if unique row exists; refuse new row when count ≥
  `max_list_entries` (return a distinct outcome, e.g. `"capped"`).
- `list_delete_sibling(scope_type, scope_key, pattern)` — delete
  the same pattern on the **other** kind.
- `list_flip_address(...)` — sibling delete + upsert (slice 11).
- `list_replace(scope_type, scope_key, kind, patterns, source, actor)`
  — transaction: delete all rows for that triple, insert the new
  set, sibling-delete each pattern on the other kind. Slice 12.

Scan only **reads**. Mutations in this slice still exist so tests
can seed rows and so 11–12 call one API.

`events.event` names used in this slice: `allowlisted`,
`blocklisted`, `list_conflict`.

---

## 5. Shared parser (locked)

One function used by scan tests (seeding), slice 11 drain, slice 12
Save. Suggested: `parse_list_line(line, *, allow_domain: bool) ->
ParsedPattern | ParseError`.

- Strip the line. Empty after strip → skip (not an error).
- If the stripped line contains any whitespace → error (leading/
  trailing already gone; internal spaces fail).
- Lowercase for the stored `pattern`.
- **Address:** exactly one `@`; local and host both non-empty; no
  other `@`. `pattern_type=address`, pattern `local@host`.
- **Domain** (only if `allow_domain`): `@host` or `host` with no `@`
  and host non-empty. Normalize to `pattern=@host`.
  `pattern_type=domain`.
- Person lists call with `allow_domain=False` (`@x.com` is an
  error, not an address).
- No wildcards (`*`) in v1 — treat `*` as invalid.

`parse_list_text(text, *, allow_domain)` walks lines, skips blanks,
returns either a de-duplicated list (first-seen order,
case-insensitive already folded) or the **first** error with
1-based line number (original file/textarea lines, including
skipped blanks so the caret matches what the admin sees).

---

## 6. Header extraction and match (locked)

Do **not** overload `parse_envelope` (~L1612), which returns From
only for learn/scan metadata.

```
iter_list_header_addrs(raw) -> list[str]
```

Parse `From`, `Sender`, `Reply-To` with `email.utils.parseaddr` /
`getaddresses` as appropriate; drop empty; lowercase; **unique,
preserve first-seen order**.

```
classify_list_hit(acc, roster, db, addrs) -> None | ListHit
```

`ListHit`: `decision` (`allow`|`block`), `pattern`, `scope`
(`person`|`domain`), `conflict: bool`.

Specificity rank:

| Rank | Match |
|---|---|
| 3 | person + address (`scope_key=acc.actual_name`) |
| 2 | domain + address (`scope_key=mailbox domain`, roster member) |
| 1 | domain + `@host` of that address |

Collect **all** hits across **all** `addrs`. Winner = maximum rank
present. If any hit at that rank is `allow` → `decision=allow`.
Else `block`. If both kinds exist at that rank → `conflict=True`
(still allow).

If `addrs` is empty, no hit.

---

## 7. `scan_inbox` integration (locked)

Site: [`scan_inbox`](../filter/filter.py) (~L1981), **after** a
numeric `score` is obtained (or reused from `prior`) and **before**
the `if score < acc.threshold` skip (~L2202).

Always score first (route-only). Then `classify_list_hit`.

**allow**

- Log `allowlist hit` (include msgid, pattern, score). In shadow
  prefix `[shadow]` like the existing would-flag line (~L2233).
- `our_action=allowlisted`.
- `log_event("allowlisted", msgid, detail=...)`.
- If `conflict`: also `log_event("list_conflict", ...)`.
- **Skip** flag / pending_move / MOVE even if `score >= threshold`.
- Bookmark: treat as **terminal** (`last_terminal = uid`).

**block**

- Treat as ≥ threshold **regardless of score** (including scores
  below `threshold`).
- Log `blocklist hit`. Events `blocklisted` (and `list_conflict` if
  set — should be rare because allow wins ties; conflict on a
  block-winning path only if implementation bug).
- Then fall through the existing `match acc.mode` path, except:
  - shadow: `our_action` may be `blocklisted_shadow` (distinct from
    `shadow`) so the dashboard can show list vs score. Do **not**
    MOVE to Junk.
  - flag/move: same STORE / pending_move as today; `our_action`
    stays `flagged` / `pending_move` / `moved_to_junk` **or** prefix
    with blocklisted in `detail` / events. Prefer keeping
    `our_action` aligned with the actual IMAP effect and putting
    `blocklisted` on the **event**, so existing action_complete
    logic (~L2217) does not break. **Locked:** event is
    `blocklisted`; `our_action` follows mode (`shadow` /
    `flagged` / `pending_move`) as today so replay still works.
    Extra: set `our_action` to those same values; the event is the
    list breadcrumb.

**no hit**

- Existing threshold path unchanged.

**Do not** call classify from `poll_junk` (~L2329).

Oversized / missing body / no Message-ID: do not list-match
(cannot parse headers reliably without body; no-msgid already
skips). Same as today’s skip.

---

## 8. Tests (locked)

New [`filter/test_address_lists.py`](../filter/test_address_lists.py).
Use tmp `STATE_DIR` like other filter tests. Fake IMAP only where
`scan_inbox` is exercised (reuse helpers from
[`filter/test_shadow_mode.py`](../filter/test_shadow_mode.py) /
[`filter/test_inbox_bookmark.py`](../filter/test_inbox_bookmark.py)).

**Loader**

- Missing `actual_name` → `SystemExit`.
- `list_domains` invalid type / duplicate domain / bad `type` →
  `SystemExit`.
- Unknown root key still rejected (`_reject_unknown_keys`).
- Roster domain lowercased; `actual_name` preserved.

**Parser**

- Trim; blank lines skipped.
- Internal space → error with line number.
- Person: `@x.com` invalid; `a@x.com` valid.
- Domain list: `x.com` and `@x.com` both → `@x.com`.
- Duplicates `A@X.com` / `a@x.com` collapse.

**Match matrix**

- Person allow address vs domain block same address → allow (rank 3).
- Domain `@host` block vs person allow address → allow.
- Domain address block vs domain `@host` allow → block (rank 2).
- Same rank allow+block → allow + `conflict`.
- Reply-To-only address on person allow, From different → hit
  (scan uses all three headers).
- Mailbox user domain not on roster → domain list ignored.

**Scan**

- Score ≥ threshold + allow → no flag/move; `our_action=allowlisted`.
- Score < threshold + block → mode action still runs (shadow: no
  MOVE).
- Shadow + block → no Inbox/Junk MOVE.

`cd filter && python -m pytest -q` stays green including the new
file.

---

## 9. Docs

[`accounts.yml.example`](../accounts.yml.example): `list_domains`
block, required `actual_name`, cap comments.

[`README.md`](../README.md) configuration reference: new keys.
Do not claim IMAP folders or dashboard editors exist until 11/12.

---

## 10. Acceptance

- Schema creates `address_lists` on `init_db` / existing DB.
- `load_accounts` (or `load_config`) requires `actual_name` and
  parses `list_domains`.
- Inbox scan allow skips junk actions; block forces the mode path
  without Bayes learn.
- `poll_junk` unchanged.
- Tests in §8 pass.
