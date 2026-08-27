# Slice 6 — Rspamd scan metadata

Architecture and implementation spec. Execute this document; do not
expand into Slices 7–8.

**Status:** implemented
**Parent plan:** sliced code-review fixes (Slice 6 of 8)
**Depends on:** Slice 2 (body fetch path is stable). Independent of 3–5.
**Primary file:** [`filter/filter.py`](../filter/filter.py) (`rspamd_scan`)
**Tests:** new [`filter/test_rspamd_scan.py`](../filter/test_rspamd_scan.py)
**Docs:** README known-limitations; comment on
[`rspamd/local.d/rbl.conf`](../rspamd/local.d/rbl.conf)

Related review: HTTP `From` is treated as envelope-from, so SPF/DMARC
ran against the IMAP recipient on every scan.

---

## 1. Goal

`/checkv2` metadata must match what the message actually is:

1. HTTP `From` is the message From address (envelope-from for SPF/DMARC).
2. HTTP `Rcpt` stays the Bayes identity (`bayes_user` or the scan
   recipient / IMAP user).
3. Do **not** send `Ip`, `Helo`, or a fake SMTP client identity.
   IMAP-originated mail has none.

---

## 2. Non-goals

Slices 7–8. Changing `rspamd_learn` (`Delivered-To` injection). Inventing
a connecting-IP from `Received:` (rspamd already walks Received when
`received = true`). Partial-MIME of oversized messages.

---

## 3. Headers (locked)

| HTTP header | Value | Notes |
|---|---|---|
| `Rcpt` | `bayes_user or recipient` | Unchanged. Per-user Bayes. |
| `From` | `parseaddr(message From)` | Omit the header if From is empty. Never copy `recipient` into `From`. |
| `Ip` / `Helo` / `User` | **absent** | Do not invent SMTP session fields. |

Parse From inside `rspamd_scan` from `raw` (reuse `parse_envelope`). Call
sites stay `rspamd_scan(raw, recipient, max_score, bayes_user=...)`.

---

## 4. RBL / SPF / DMARC (locked)

[`rbl.conf`](../rspamd/local.d/rbl.conf) has `from = true` and
`received = true`. Without an `IP` header only the Received-chain path
can fire. Document that RBL, SPF, and DMARC are **degraded** on this
IMAP path: they depend on Received headers in the message plus
Bayes/fuzzy/neural. Do not fabricate an IP to make `from` lookups look
complete.

---

## 5. Tests

Capture `requests.post` kwargs on a fake `/checkv2`:

1. Raw From `sender@example.com`, recipient `u@example.com`,
   `bayes_user=pool` → `From == sender@...`, `Rcpt == pool`, no `Ip`/`Helo`.
2. Empty/missing From → no `From` header; `Rcpt` still set.
3. No `bayes_user` → `Rcpt == recipient`.

---

## 6. Acceptance

`cd filter && python -m pytest -q` passes. README states IMAP scoring
has no SMTP client IP. `rspamd_scan` never sets `From` to the recipient.
