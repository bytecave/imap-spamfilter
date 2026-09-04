# Slice 9 — One VPS Bayes

Architecture and implementation spec. Execute this document; do not
expand into Slices 10–12.

**Status:** ready to implement
**Parent plan:** [`allow_block_sliced_plan.md`](allow_block_sliced_plan.md)
(Slice 9 of 9–12)
**Depends on:** none. Independent of list slices 10–12.
**Primary files:** gitignored live `accounts.yml`;
[`accounts.yml.example`](../accounts.yml.example);
[`README.md`](../README.md) (Bayes identity section)
**Tests:** none new unless an example YAML fixture must stay valid
**Classifier config:** do **not** change
[`rspamd/local.d/classifier-bayes.conf`](../rspamd/local.d/classifier-bayes.conf)
(`users_enabled = true`, `min_learns = 200`, `autolearn = false`)

Today every mailbox is its own Bayes notebook: `bayes_user` is unset, so
[`rspamd_scan`](../filter/filter.py) (~L1341) and `rspamd_learn` key on
`acc.user`. Steve’s Train-* does not help Bobbi. This slice puts the
whole VPS on one notebook.

---

## 1. Goal

Every account processed by this ByteLord instance shares one rspamd
Bayes identity so every explicit learn (Train-* drain, Inbox↔Junk
moves, `bootstrap_train.py`) feeds the same classifier.

Leave `users_enabled = true`. Sharing is done with the existing
`bayes_user` override (HTTP `Rcpt` on scan, `Delivered-To` on learn),
not by turning users off. That keeps a later split possible without a
rspamd redesign.

---

## 2. Non-goals

- Slices 10–12 (allow/block).
- Changing `BUILTIN_DEFAULTS["bayes_user"]` in
  [`filter/filter.py`](../filter/filter.py) (~L258). Other deployments
  stay per-mailbox when they omit the key. **Only** ByteLord
  `accounts.yml` `defaults:` sets the shared token.
- Merging existing Redis per-recipient tokens into the new key.
- Writing a new bulk-train app (see §5).
- Altering `min_learns`, autolearn, or fuzzy/neural.

---

## 3. Config (locked)

Live gitignored `accounts.yml` (VPS path
`/opt/bytelord/projects/imap-spamfilter/accounts.yml`):

```yaml
defaults:
  bayes_user: bytelord
```

Per-account `bayes_user` must stay **unset** so every account inherits
the default. Token rules are already enforced by `_clean_bayes_user`
(~L306): non-empty, no CR/LF.

After the YAML change, `docker restart spamfilter` (or compose
recreate). Existing `rspamd_scan(..., bayes_user=acc.bayes_user or
acc.user)` needs **no code change**.

[`accounts.yml.example`](../accounts.yml.example): show the same
`defaults.bayes_user` pattern in comments (ByteLord / single-site
pooling). Keep the existing `marcel-pool` example as the “subset of
accounts share, family stays isolated” story.

---

## 4. Cutover semantics (locked)

`min_learns = 200` is **per notebook**. Switching `bayes_user` starts
a **fresh** namespace. Old tokens remain in Redis under the previous
keys (`acc.user` strings) and are **no longer consulted**. Do not
script a Redis merge in this slice.

Until the shared notebook reaches ~200 spam and ~200 ham learns,
Bayes contributes little; rspamd’s other symbols still score. Lists
(slices 10–12) are a separate safety rail.

README must say this explicitly next to the existing “Switching an
existing account from per-recipient to a `bayes_user` value…”
paragraph (~L554).

---

## 5. Follow-up: re-feed Trained-* (not this slice’s code)

All previously learned mail still sits in each account’s
`Trained-Spam` / `Trained-Ham` (under the server Junk parent; Outlook
may display “Junk Email”). After `bayes_user` is live, re-learn those
bodies into the new notebook.

**Preferred (no folder churn):** existing
[`filter/bootstrap_train.py`](../filter/bootstrap_train.py). Point
`source` at the real IMAP `Trained-*` name, kind `spam` or `ham`,
**omit `--move-to`** so messages stay in Trained-*.

```bash
docker exec spamfilter python bootstrap_train.py steve_rjmetalfab \
  'Junk/Trained-Spam' spam
docker exec spamfilter python bootstrap_train.py steve_rjmetalfab \
  'Junk/Trained-Ham' ham
```

Use the **actual** IMAP name from SPECIAL-USE remap (may be
`Junk Email/Trained-Spam`). Repeat per account in `accounts.yml`.
`bootstrap_train` already uses `acc.bayes_user or acc.user`.

**Alternative:** MOVE `Trained-*` → `Train-*` and let the running
filter drain (`max_train_per_run` is 5000 on live ByteLord YAML). Mail
returns to `Trained-*` after learn. More churn; same tokens.

**Do this only after** `defaults.bayes_user` is set. Doing it first
writes into the old per-mailbox notebooks.

**Shadow:** retention is off, so Trained-* is safe until promotion.
If accounts leave `shadow` before re-feed, aged Trained-* can MOVE to
Trash (`trained_retention_days`). Re-feed first, or raise retention.

No new application. A one-command wrapper over all accounts is
optional later, not slice 9.

---

## 6. Docs

[`README.md`](../README.md) “Bayes identity” (~L523):

- Add a “single-site / one VPS notebook” example (`defaults.bayes_user`).
- State that ByteLord uses one shared identity; allow/block lists
  (slices 10–12) are independent of this knob.
- Point at §5 for Trained-* re-feed after cutover.
- Keep the orphaned-key warning.

[`accounts.yml.example`](../accounts.yml.example): comment the
`defaults.bayes_user: bytelord` pattern and the “omit per-account
bayes_user so everyone inherits” rule.

---

## 7. Tests

No new tests required. Existing
[`filter/test_rspamd_scan.py`](../filter/test_rspamd_scan.py) already
covers `bayes_user` on `Rcpt`. Do not change fixtures unless an
example YAML parse test starts failing because of comments.

---

## 8. Acceptance

- Live ByteLord `accounts.yml` `defaults.bayes_user` is set; no
  per-account override.
- `classifier-bayes.conf` unchanged (`users_enabled = true`).
- `BUILTIN_DEFAULTS["bayes_user"]` still `None`.
- README + `accounts.yml.example` describe cutover, orphaned Redis
  keys, and Trained-* re-feed via `bootstrap_train.py`.
- After restart, scan/learn use `Rcpt` / `Delivered-To` `bytelord`
  (or the chosen token), not each mailbox address.
