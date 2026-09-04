# Slice 12 — Dashboard list UI

Architecture and implementation spec. Last slice in the allow/block
plan.

**Status:** ready to implement
**Parent plan:** [`allow_block_sliced_plan.md`](allow_block_sliced_plan.md)
(Slice 12 of 9–12)
**Depends on:** Slice 10 (roster, parser, `address_lists`,
`list_replace`). Slice 11 is **not** required (admin can type lists
before IMAP folders exist).
**Primary files:** [`filter/dashboard.py`](../filter/dashboard.py);
new [`filter/lists.js`](../filter/lists.js) shipped next to
[`filter/favicon.png`](../filter/favicon.png) (Dockerfile COPY
already copies `filter/`).
**Tests:** extend [`filter/test_dashboard.py`](../filter/test_dashboard.py)
**Docs:** README dashboard section; footer no longer “read-only”
for the whole app.

This is the first **write** UI besides login/logout. Auth stays the
existing signed session. No OAuth2. Admin only. Netbird + Caddy
`spam.bytelord.net` is later ops, not this slice.

---

## 1. Goal

Admin can view and edit:

- **Domain lists** — one roster domain at a time, Allow or Block,
  addresses and `@host`.
- **User lists** — one `actual_name` at a time, Allow or Block,
  addresses only.

UX: one textarea, one pattern per line, Save/Cancel, find-in-text
search that does not hide lines, unsaved-change warnings.

---

## 2. Non-goals

- Non-admin list editors (Steve uses IMAP folders from slice 11).
- Adding/removing `list_domains` from the UI (YAML roster only).
- OAuth2, Caddy, Netbird.
- `'unsafe-inline'` scripts.
- Editing Bayes or accounts.yml from the dashboard.

---

## 3. Authz (locked)

- Nav links “Domain lists” and “User lists” render **only** if
  `_current_scope()[0]` is admin ([`BASE`](../filter/dashboard.py)
  ~L785).
- GET `/lists/domains` and `/lists/users` (names may vary; pick
  these): non-admin → **403**.
- POST Save: admin-only; non-admin → 403.
- Existing `_requires_auth` still applies (login redirect).

---

## 4. CSRF, body size, DB (locked)

**CSRF.** Generate a token into `session` (reuse-or-set on GET).
Save form includes `csrf_token`. POST compares with
`hmac.compare_digest`. Mismatch → 400, no writes. Token rotates
on login (existing session already binds credentials).

**Body size.** `app.config["MAX_CONTENT_LENGTH"]` is
`LOGIN_REQUEST_MAX` (16 KiB) (~L323). 1000 list lines can exceed
that. Raise the limit **only** on list POST views (Flask
`before_request` or a dedicated blueprint max), e.g. **256 KiB**,
without loosening `/login`.

**SQLite.** [`_db()`](../filter/dashboard.py) (~L476) stays
`mode=ro` for Summary/Messages/etc. Add `_db_rw()` for list GET
(optional; GET can stay ro) and list POST: normal URI, WAL,
`busy_timeout`, `row_factory=Row`. Never use rw on unrelated
routes.

**Events.** `list_replace` logs `list_dashboard_save` with
`account="_dashboard"` (slice 10) and `actor` = session username.
Sibling deletes are part of the same save (no extra event
required; detail may say `flipped=N`).

---

## 5. CSP and JavaScript (locked)

Slice 8 CSP (~L335):

```
default-src 'none'; style-src 'unsafe-inline'; img-src 'self';
form-action 'self'; base-uri 'none'
```

There is **no** `script-src`, so `default-src 'none'` blocks JS.
The agreed UX needs JS (`beforeunload`, dirty tracking, search,
caret on error).

**Locked CSP delta:** add `script-src 'self'`. Keep
`style-src 'unsafe-inline'`. Do **not** add `'unsafe-inline'` for
scripts.

Serve [`filter/lists.js`](../filter/lists.js) as GET `/lists.js`
(`send_file`, same pattern as `/favicon.png` ~L845). Page includes
`<script src="/lists.js" defer></script>`. Dockerfile already
copies the filter package; confirm `lists.js` is in the image.

---

## 6. Page chrome (locked)

Two tabs, same layout.

**Fixed header** (does not scroll away):

1. Dropdown — Domain tab: roster domains from YAML (label may
   include `company` / `personal`). Include `bytelord.net` even
   with zero mailboxes. User tab: distinct `actual_name` values
   from loaded `accounts.yml`, sorted A–Z (not from SQLite).
2. Allow | Block toggle (single select, not both textareas).
3. Save and Cancel — **disabled** until the textarea differs from
   the snapshot loaded for this dropdown+toggle.

**Scrollable body:**

4. Search input + (x) clear. Does **not** delete or hide textarea
   lines. On input: select/jump to the first matching line; show
   “N matches” or “no matches” beside the box. Empty query: no
   jump, full text remains.
5. Textarea: one pattern per line, current SQLite list for
   `(scope, kind)`.

**Navigation warnings (JS):** if dirty, `confirm()` and **stay**
when:

- changing the domain/user dropdown
- switching Allow | Block
- clicking the other lists tab (or any `nav a`)
- `beforeunload` (browser leave / refresh)

Cancel restores the snapshot and clears dirty. Save POST then
GET-redirect to the same dropdown+kind (PRG) so a clean snapshot
loads.

Roster is **not** editable on the page.

---

## 7. Save rules (locked)

Server uses slice 10 `parse_list_text`:

- Domain tab: `allow_domain=True`.
- User tab: `allow_domain=False` (`@host` / bare host → error).

On first error: HTTP 400, **no writes**, re-render with the
submitted text, message, and `data-error-line` (1-based). JS
places the caret on that line.

On success, one transaction:

1. `list_replace` for `(scope_type, scope_key, kind)` with
   normalized unique patterns.
2. Sibling delete for each pattern on the other kind (IMAP flip
   semantics).
3. Refuse if resulting count > `max_list_entries` (use the
   **defaults** cap from config; dashboard may read
   `BUILTIN_DEFAULTS` / loaded defaults — if accounts have
   per-account overrides, use `defaults` merge from YAML, not
   min() across accounts). Locked: cap = YAML `defaults` /
   builtin `max_list_entries` (1000).

POST fields: `scope_key`, `kind`, `body`, `csrf_token`. Reject
unknown `kind`. Domain `scope_key` must be on the roster (404).
User `scope_key` must be an `actual_name` present in accounts.yml
(404). Do not accept forged `scope_type` that disagrees with the
route.

---

## 8. Roster loading in the dashboard

Dashboard already reads [`CONFIG_PATH`](../filter/dashboard.py)
`/app/accounts.yml` in `_known_accounts()` (~L1325). Slice 10’s
`load_config` / roster helper should be imported so Domain
dropdown and 404 checks use the same parser as the filter. If
YAML is missing, list pages show an error card (no silent empty
roster that allows POST to arbitrary domains).

---

## 9. Footer / copy

[`BASE`](../filter/dashboard.py) footer `read-only` (~L802) is
false once this ships. Change to something accurate, e.g.
`imap-spamfilter dashboard` without “read-only”, or
“list edits: admin only”.

Module docstring (~L1) still says read-only — update.

---

## 10. Tests (locked)

Extend [`filter/test_dashboard.py`](../filter/test_dashboard.py):

1. Admin GET domain and user list pages → 200; contains
   `/lists.js`.
2. Non-admin GET → 403; nav HTML for that user has no Domain/User
   lists links (if the test client can log in as scoped user).
3. POST without CSRF → 400; table unchanged.
4. POST invalid line (internal space) → 400; no persist.
5. POST person list `@x.com` → 400.
6. POST domain list `x.com` → stored as `@x.com`.
7. POST allow `a@x.com` while block has `a@x.com` → block sibling
   gone.
8. POST `scope_key` not on roster / not an `actual_name` → 404.
9. Non-admin POST → 403.

JS behavior is not unit-tested beyond the script tag. Optional
playwright is out of scope.

`cd filter && python -m pytest -q` passes.

---

## 11. Acceptance

- Admin can edit person and domain lists from the textarea UX
  specified in the parent plan.
- Non-admins cannot.
- YAML roster is the only domain inventory.
- CSP allows only `script-src 'self'` for this feature.
- Unsaved navigation is guarded in JS; Save validation matches
  slice 10 parser including caret line.
- Login size limit unchanged.

---

## 12. Hybrid note

IMAP (slice 11) persists immediately (no Save button). Dashboard
Save is the only batched editor. Document that in README so
operators do not expect Outlook drags to wait for Save.
