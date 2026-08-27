# Slice 4 — Connection and config correctness

Architecture and implementation spec. Execute this document; do not
expand into Slices 5–8.

**Status:** implemented
**Parent plan:** sliced code-review fixes (Slice 4 of 8)
**Independent of:** Slices 2–3. Needed before any real IMAP LOGIN and
before `email-oauth2-proxy` (`tls_mode: none` on localhost).
**Primary files:** [`filter/filter.py`](../filter/filter.py),
[`filter/bootstrap_train.py`](../filter/bootstrap_train.py)
**Tests:** new [`filter/test_connection.py`](../filter/test_connection.py);
update `_mk_account` in [`filter/test_shadow_mode.py`](../filter/test_shadow_mode.py)
and [`filter/test_learn.py`](../filter/test_learn.py) for new Account fields.
**Docs:** README connection table, [`accounts.yml.example`](../accounts.yml.example)

Related review write-up:
[`spamfilter_discussion_and_code_review.md`](spamfilter_discussion_and_code_review.md)
(`ssl: false` is not STARTTLS; YAML `"false"` is truthy; IDLE fallback
calls `idle()` anyway).

---

## 1. Goal

1. TLS behaviour matches documentation. `ssl: false` must not LOGIN
   over plaintext.
2. Quoted YAML `"false"` must not become Python `True`.
3. Servers without IMAP IDLE must poll with `poll_interval`, not call
   `client.idle()`.

---

## 2. Non-goals

| Later slice | Do not touch now |
|---|---|
| 5–8 | Message-ID PK, rspamd From/Rcpt, ops, dashboard |
| OAuth | Deploying `email-oauth2-proxy` (follow-on after this slice) |

Also out of scope: auto-changing `imap_port` when `tls_mode` changes
(operator sets port). Default remains 993 + implicit TLS.

---

## 3. TLS modes (locked)

Replace `Account.ssl: bool` with:

| `tls_mode` | Transport | LOGIN |
|---|---|---|
| `implicit` | IMAPS (`IMAPClient(..., ssl=True)`) | after TLS |
| `starttls` | plain connect, then `starttls(ssl_context=)` | after STARTTLS |
| `none` | plaintext | only if host is loopback **or** `allow_insecure_tls: true` |

Default: `tls_mode: implicit`. New field `allow_insecure_tls` default
`false`.

**Deprecated `ssl:` alias** (only if the *user* YAML set `ssl`, not the
builtin default):

- `ssl: true` → `implicit` + WARNING
- `ssl: false` → `starttls` + WARNING (this is the documented intent)
- If both `tls_mode` and `ssl` are set, `tls_mode` wins; WARNING that
  `ssl` is ignored

Refuse `tls_mode: none` in `validate_account` unless
`_host_is_loopback(imap_host)` or `allow_insecure_tls`. Loopback is
literal hostname/IP (`localhost`, `127.0.0.1`, `::1`, `[::1]`), **not**
a DNS lookup.

Shared helper `connect_imap(acc) -> IMAPClient` used by `_run_account`
and `bootstrap_train.py` (login included). One TLS implementation.

---

## 4. YAML booleans (locked)

`bool("false")` is `True`. Parse with `_parse_bool`:

True: `True`, `"true"`, `"yes"`, `"on"`, `"1"` (case-insensitive), `1`
False: `False`, `"false"`, `"no"`, `"off"`, `"0"`, `0`

Anything else → `ValueError`. `load_accounts` catches `ValueError` /
`KeyError` / `TypeError` as `SystemExit`, matching other validation.

Apply to: `ssl` (alias), `learn_from_moves`, `auto_special_folders`,
`allow_insecure_tls`.

---

## 5. IDLE fallback (locked)

Today `idle_cap = client.has_capability("IDLE")` is logged and ignored;
`client.idle()` always runs. `poll_interval` is never read.

After Slice 4:

- **IDLE present:** unchanged — `idle()` / `idle_check` with
  `wait = min(idle_timeout, max(30, junk_poll_interval))`.
- **IDLE absent:** do **not** call `idle()`. `time.sleep` in 30s chunks
  for `max(1, acc.poll_interval)` so SHUTDOWN is observed. Log the
  warning with `poll_interval`, not `junk_poll_interval`.

Extract `wait_between_scans(client, acc, *, idle_cap)` so tests can
invoke it without the whole account loop.

---

## 6. Tests

[`filter/test_connection.py`](../filter/test_connection.py)

1. `ssl: "false"` (quoted) → `tls_mode == "starttls"` (not implicit).
2. `tls_mode: none` + `imap_host: imap.example.com` → `SystemExit`.
3. `tls_mode: none` + `imap_host: 127.0.0.1` → loads.
4. `tls_mode: none` + remote host + `allow_insecure_tls: true` → loads.
5. `learn_from_moves: "false"` → `False`.
6. FakeIMAP `idle_cap=False`: `wait_between_scans` never calls `idle()`.
   Monkeypatch `time.sleep`. `poll_interval` small (e.g. 1).
7. FakeIMAP `idle_cap=True`: `idle()` is called.

Temp YAML files via `tmp_path`; call `load_accounts`.

---

## 7. Acceptance criteria

1. `cd filter && python -m pytest -q` passes.
2. README no longer claims `ssl: false` is STARTTLS; it documents
   `tls_mode`.
3. `bootstrap_train.py` uses `connect_imap`.
4. Existing Slice 1–3 tests updated for `tls_mode` on Account.

---

## 8. Suggested edit order

1. `_parse_bool`, `_host_is_loopback`, `_resolve_tls_mode`,
   `connect_imap`, `wait_between_scans`.
2. Account fields + `load_accounts` / `validate_account`.
3. `_run_account` + `bootstrap_train.py`.
4. Tests + `_mk_account`.
5. README + `accounts.yml.example`.
