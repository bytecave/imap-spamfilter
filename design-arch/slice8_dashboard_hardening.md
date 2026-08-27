# Slice 8 — Dashboard hardening + log redaction

Architecture and implementation spec. Last slice in the parent plan.

**Status:** implemented
**Parent plan:** sliced code-review fixes (Slice 8 of 8)
**Independent of:** Slices 1–7 (touches dashboard + exception logging only)
**Primary files:** [`filter/dashboard.py`](../filter/dashboard.py),
[`filter/filter.py`](../filter/filter.py) (`redact_log` + IMAP except
sites), [`unraid/spamfilter.xml`](../unraid/spamfilter.xml),
[`docker-compose.yml`](../docker-compose.yml), README
**Tests:** [`filter/test_dashboard.py`](../filter/test_dashboard.py),
[`filter/test_log_redact.py`](../filter/test_log_redact.py)
**Out of scope:** OAuth proxy, container lockdown, new dashboard features.

---

## 1. Goal

1. The dashboard works on the default Unraid path (controller password
   from `state/controller.password`, not only `RSPAMD_PASSWORD`).
2. Missing scope is fail-closed. Legacy `DASHBOARD_USER` is masked.
3. Login, session, headers, and secret-file writes are hardened.
4. IMAP/exception log lines cannot echo LOGIN passwords.

---

## 2. Non-goals

OAuth. Binding the *container* to 127.0.0.1 (breaks Docker port-map).
Changing Unraid's host port mapping. JS in the dashboard.

---

## 3. Must-fix (locked)

**Controller password.** `_rspamd_stats()` uses
`filter._load_rspamd_password()` (lazy import to avoid a cycle). Env
`RSPAMD_PASSWORD` still wins; else `state/controller.password`.

**Scope.** `_parse_user_line`: `username:verifier` with no third field
is **skipped** (log a warning). Explicit `:admin` required. The
interactive helper already writes `:admin`. Legacy
`DASHBOARD_USER`+`DASHBOARD_PASSWORD` stays admin (that pair is an
explicit admin account).

**Unraid.** `DASHBOARD_USER (legacy)` `Mask="true"`.

---

## 4. Hardening (locked)

**Login backoff.** In-memory, process-local. Key `(client IP, username
lowercased)`. After 5 failures in 60s, further attempts run the dummy
pbkdf2 (timing) then return the same "Invalid username or password."
text (HTTP 200, do not advertise lockout). Success clears the key.
IP from `X-Forwarded-For` first hop if present, else `remote_addr`.

**Headers** (`@app.after_request` on every response):

- `Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline'; img-src 'self'; form-action 'self'; base-uri 'none'`
- `X-Content-Type-Options: nosniff`
- `X-Frame-Options: DENY`
- `Referrer-Policy: no-referrer`
- `Cache-Control: no-store`

**Session.** On login store `issued` and `last` (unix seconds). Idle
**8h**, absolute **24h**. `_requires_auth` clears and redirects to
login if either elapsed. Refresh `last` on each authenticated request.

**Logout is POST.** Nav uses a small POST form. GET `/logout` returns
405 and does not clear the session.

**Secret files.** `_load_secret` and the users-file helper write with
`os.open(..., 0o600)` (mode on create, `fchmod` 0o600). Do not
`write_text` then `chmod` after the file is world-readable.

**`_kpi`.** `label`, `value`, and `sub` go through `_h()`. `cls` is
restricted to `[A-Za-z0-9_-]`.

**`?next=`.** After login, allow only `^/[A-Za-z0-9/_-]*$`. Anything
else (including `//host`, scheme-relative, `/?q=`) becomes `/`.

**Bind.** Container still listens on `0.0.0.0:8080`. Compose example
and README tell operators to publish `127.0.0.1:8080:8080` on a VPS
(not `8080:8080`). Unraid keeps its existing host-port mapping
(LAN WebUI).

---

## 5. Log redaction (locked)

Add `redact_log(text, *secrets, limit=200)` in `filter.py`:

1. Replace each non-empty secret (account password, `RSPAMD_PASSWORD`)
   with `***`.
2. Replace `\bLOGIN\b` and the rest of that line with `LOGIN ***`
   (IMAP servers that echo the LOGIN command).
3. Truncate to `limit`.

Use it at every `log.warning` / `log.error` / `log_event(detail=)` that
interpolates an IMAP or generic exception (`ex`), passing
`acc.password` when `acc` is in scope. Truncation-only `str(ex)[:300]`
is not enough.

---

## 6. Tests

Dashboard (`test_dashboard.py`):

1. `_parse_user_line("u:hash")` does not insert a user; `"u:hash:admin"`
   does (admin); `"u:hash:acct1|acct2"` is non-admin with those accounts.
2. Flask test client: login with `?next=//evil.example` and
   `?next=https://evil.example` → `Location` is `/`. `?next=/messages`
   is allowed.
3. `_kpi` with `value="<script>"` does not contain a raw `<script>`
   tag.

Log redaction (`test_log_redact.py`):

1. `redact_log("LOGIN user secretpass", "secretpass")` contains `***`
   and not `secretpass`.
2. A string with `LOGIN "u" "pw"` is reduced to start with `LOGIN ***`.

---

## 7. Acceptance

`cd filter && python -m pytest -q` passes. README: missing scope is
ignored; VPS bind `127.0.0.1`; hashed-users file preferred. GET
`/logout` does not clear the session.
