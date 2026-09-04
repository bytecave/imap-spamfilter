# New requirements — Allowlist / Blocklist

**Status:** superseded for implementation by design-arch slices 9–12  
**Captured:** 2026-08-29  
**Priority driver:** R&J Metal Fab business mail (P.O.s / invoices) must not be lost to Junk; a single false positive can cost tens of thousands of dollars.  
**Supersedes (for this feature):** README “Known limitations → No allowlist” once slices 10–12 ship.

This document is the original product-requirements capture. It is
**not** the implementation spec. Locked behavior, schema, IMAP
contracts, dashboard UX, and tests live in:

- [`design-arch/allow_block_sliced_plan.md`](design-arch/allow_block_sliced_plan.md) — index and locked decisions
- [`design-arch/slice9_shared_bayes.md`](design-arch/slice9_shared_bayes.md) — one VPS Bayes notebook
- [`design-arch/slice10_list_core.md`](design-arch/slice10_list_core.md) — YAML roster, SQLite, matching, Inbox scan
- [`design-arch/slice11_imap_list_folders.md`](design-arch/slice11_imap_list_folders.md) — `INBOX/Allowlist` / `Blocklist`
- [`design-arch/slice12_dashboard_lists.md`](design-arch/slice12_dashboard_lists.md) — admin list editor

Where this file disagrees with those slices (person vs domain drag
scope, headers, dashboard layout, Bayes pooling), **the slices win**.

---

## 1. Goal

Add **server-side allowlists and blocklists** so operators can force
obvious ham (and block obvious spam) without relying solely on Bayes /
rspamd scores. Lists are editable via:

1. **IMAP folder gestures** (drag mail into Inbox subfolders), and  
2. **Dashboard UI** (manual edit, including whole-domain entries).

Scoring still runs; list hits **override** the spam/ham decision for
routing (exact override semantics TBD in the design slice — e.g. force
keep-in-Inbox vs force-to-Junk, and whether Bayes still learns).

---

## 2. List scopes

### 2.1 Domain-scoped lists (shared mailboxes)

For accounts whose mailbox domain is in a **config-editable restriction
list**, allow/block entries are **per domain** (shared across all
accounts on that domain).

**Initial allowed domains** (must be editable without a code change):

- `bytecave.net`
- `eizenhoefer.net`
- `rjmetalfab.com`

**Planned future addition (config only):** `bytelord.net`

When a user on e.g. `steve@rjmetalfab.com` allowlists a sender, that
entry applies to **all** `*@rjmetalfab.com` accounts processed by this
filter instance.

### 2.2 Personal lists (consumer domains)

When support for `gmail.com` and `live.com` (and similar) is added,
accounts in those domains get **personal** allow/block lists (scoped to
that account / mailbox identity), **not** domain-wide lists.

### 2.3 Config for which domains use domain-scoped lists

Operators must be able to edit the set of domains that use
**domain-scoped** (vs personal) lists in a **config file** (e.g. a key in
`accounts.yml` defaults, or a dedicated file under the data/secrets
layout). Adding `bytelord.net` later must not require a code deploy.

Exact config key name and file location: TBD in design slice.

---

## 3. IMAP folder UX

Under each account’s **Inbox**, maintain two subfolders:

| Folder | Purpose |
| --- | --- |
| `Inbox/Allowlist` | Dragging a message here adds its sender (and/or domain — see §5) to the allowlist for that account’s list scope |
| `Inbox/Blocklist` | Same for the blocklist |

### 3.1 Behavior when mail is dragged in

1. Detect new messages in `Inbox/Allowlist` or `Inbox/Blocklist` (poll
   or IDLE on those folders — TBD).
2. Derive list entry from the message (sender address and/or domain;
   syntax in §5).
3. Upsert into the server-side SQLite list for the correct scope
   (domain list vs personal list per §2).
4. Post-action handling of the dragged message: TBD (e.g. move back to
   Inbox, move to Trained-Ham/Spam, leave in place, or delete from the
   list folder after learn). Prefer an operator-safe default that does
   not lose the mail.

### 3.2 Folder lifecycle

- Auto-create `Inbox/Allowlist` and `Inbox/Blocklist` when the account
  connects (similar to Train-* folder creation), unless design chooses
  SPECIAL-USE or another parent.
- Folder names must be delimiter-aware (`/` vs `.`) like existing
  folder remapping.
- Shadow-mode policy: TBD — likely **allow** list-folder CREATE and
  list updates even in `shadow`, since this is operator training of
  policy, not automatic Junk moves. Document the hybrid contract
  explicitly in the design slice.

---

## 4. Storage

- All allow/block lists live **server-side in the filter SQLite
  database** (same state DB used today under
  `/opt/bytelord/data/imap-spamfilter/state/`, or successor path).
- Lists must survive container restarts and be included in backup
  guidance (state DB is already a backup asset).
- Schema should support:
  - scope type: `domain` | `account` (personal)
  - scope key: domain name or account name / mailbox user
  - list kind: `allow` | `block`
  - entry pattern (see §5)
  - audit metadata: created_at, updated_at, source (`imap` | `dashboard` | `config`), optional actor/account that added it, optional message-id of the sample mail

---

## 5. Entry syntax

Lists must support at least:

| Pattern | Meaning |
| --- | --- |
| `user@example.com` | Exact mailbox address |
| `@example.com` or `example.com` | Entire domain (operator must be able to add whole domains manually) |

Additional patterns (optional, design may defer):

- Wildcard local-part (e.g. `*@vendor.com`) if distinct from bare domain
- Subdomain rules (e.g. `@*.example.com`) — only if needed; prefer
  simple exact + whole-domain first

**Conflict rules (TBD in design):** if an address is on both allow and
block, which wins? Recommendation to evaluate: **block beats allow**, or
**most specific match wins** (address > domain).

**Precedence vs rspamd score (TBD):** allow → never treat as spam for
move/flag; block → treat as spam (or force Junk) regardless of score.
Shadow mode should still log `[shadow] allowlist hit` / `blocklist hit`.

---

## 6. Dashboard

Add a dashboard tab (or page) to **view and edit** allow/block lists.

### 6.1 Required UI

- **Dropdown** to select which list to edit, covering:
  - each configured **domain** list (allow and block — either one
    dropdown of “rjmetalfab.com · Allow”, “rjmetalfab.com · Block”, …
    or domain dropdown + allow/block toggle)
  - each **personal** list once gmail/live accounts exist
- Display current entries
- Add / remove entries using the §5 syntax (including whole-domain)
- Auth/scope: **admin** sees all lists; restricted dashboard users (if
  any) should only see lists for domains/accounts they are scoped to
  (align with existing dashboard scope model)

### 6.2 Read-only today

The dashboard is currently read-only by product policy. This feature
**explicitly adds write actions** for list management. Design must
cover CSRF, session auth, audit logging, and that list edits are the
only mutations (still no arbitrary mailbox MOVE from the UI unless
separately approved).

---

## 7. Scoring / filter integration

When scanning Inbox (and possibly Junk poll):

1. Resolve the account’s list scope (domain vs personal).
2. Match From (and possibly Reply-To / Return-Path — TBD) against allow
   then block (or per conflict rule).
3. Apply override before or instead of threshold-based flag/move.
4. Log clearly for dashboard Messages view (e.g. action
   `allowlisted` / `blocklisted`).

Out of scope until design: whether allowlisted mail still updates Bayes;
whether blocklisted mail is auto-learned as spam.

---

## 8. Non-goals (for the first implementation)

- Replacing Bayes / rspamd scoring entirely
- Client-side or per-device lists
- Syncing lists to Microsoft 365 / Google “safe senders” junk policies
  (server-side filter lists only)
- Allowlisting by subject keywords or attachment type (address/domain
  only for v1)
- Public internet exposure of the dashboard beyond existing
  loopback/reverse-proxy model

---

## 9. Acceptance criteria (high level)

1. Config file lists which domains use **domain-scoped** lists;
   `bytelord.net` can be added by config edit alone.
2. For `rjmetalfab.com` (and the other configured domains), an
   allow/block entry added from any account on that domain is visible
   and enforced for all accounts on that domain.
3. Dragging a message into `Inbox/Allowlist` or `Inbox/Blocklist`
   creates a durable SQLite entry and does not lose the message.
4. Dashboard tab can select a list via dropdown and add/remove
   `user@host` and whole-domain entries.
5. Future `gmail.com` / `live.com` accounts use **personal** lists, not
   domain-wide lists.
6. In `shadow`, list hits are logged; Inbox/Junk/Trash are not mutated
   by automatic spam actions (list-folder maintenance policy TBD but
   documented).
7. Tests cover scope resolution, syntax parsing, conflict rules, IMAP
   folder drain, and dashboard authz for list edits.
8. README “No allowlist” limitation is removed/updated when shipped.

---

## 10. Open questions for the design slice

**Resolved.** See [`design-arch/allow_block_sliced_plan.md`](design-arch/allow_block_sliced_plan.md)
“Locked product decisions.” Historical questions left below for
traceability only.

1. Exact override semantics vs threshold / Bayes learn.
2. Allow vs block conflict and address-vs-domain precedence.
3. Which header(s) are matched (From only vs Reply-To / Sender).
4. What happens to the message after a successful list-folder drag.
5. Config file location and schema for the domain-restriction list.
6. Whether domain lists are global to the filter instance or per
   “organization” if multiple tenants ever share one DB.
7. Folder naming under non-English / localized Inbox display names.
8. Rate limits / abuse if someone floods Allowlist with mail.
9. Migration: empty tables on upgrade; no backfill from M365 safe
   senders unless separately requested.
10. Interaction with `bayes_user` shared pools (list scope is still
    domain/account as above, independent of Bayes identity).

---

## 11. Motivation snapshot

Early Bayes (~463 spam / ~571 ham) scored a same-person test
(`rich@bytecave.net` → `rich@eizenhoefer.net`) around **+6.9** (under
default threshold 8.0). Training remains the primary quality path;
allow/block is a **safety rail** for business-critical domains
(especially `rjmetalfab.com`) where false positives are unacceptable.
