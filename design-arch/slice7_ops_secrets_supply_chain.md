# Slice 7 — Ops / secrets / supply chain

Architecture and implementation spec. Execute this document; do not
expand into Slice 8.

**Status:** implemented
**Parent plan:** sliced code-review fixes (Slice 7 of 8)
**Independent of:** Slices 2–6. Needed before a multi-account VPS deploy.
**Primary files:** [`unraid/bootstrap.sh`](../unraid/bootstrap.sh),
[`filter/filter.py`](../filter/filter.py) (`_apply_env_overrides`),
[`docker-compose.yml`](../docker-compose.yml), [`filter/Dockerfile`](../filter/Dockerfile),
Unraid XMLs, README, [`.env.example`](../.env.example)
**Tests:** [`filter/test_env_overrides.py`](../filter/test_env_overrides.py)
**Out of scope:** dashboard hardening (Slice 8), OAuth proxy deploy.

---

## 1. Goal

1. Bootstrap does not leak secrets on `ps`, does not treat a partial
   download as success, and can pick up template fixes after the first
   run.
2. YAML `defaults:` for retention is not silently overwritten by the
   Unraid template env vars.
3. Third-party images are version-pinned. Production secrets live at
   `/opt/bytelord/secrets/imap-spamfilter.env`, not a compose-adjacent
   `.env`.
4. Redis `noeviction` is documented as intentional.

---

## 2. Non-goals

Slice 8. Changing `junk_retention_days` default from 10. Pinning GitHub
Actions to commit SHAs (optional in the parent plan; skip). Pinning the
filter image away from `:latest`. Redis LRU.

---

## 3. Bootstrap (locked)

**Local copy first.** `SCRIPT_DIR/..` is the checkout. If
`rspamd/local.d/<file>` (etc.) exists there, `cp` it. Curl GitHub only
when the file is absent from the checkout.

**Curl pin.** Never `.../main`. Checkout-backed installs are preferred.
A no-checkout install must explicitly set `SPAMFILTER_REF` to the same
40-character commit used to obtain the script; there is no embedded stale
fallback. `SPAMFILTER_REPO` defaults to `marcelverdult/imap-spamfilter`.

**Atomic fetch.** `curl -o dest.tmp` then `mv`. A failed/interrupted
curl must not leave a dest that later runs treat as present.

**Version stamp.** `unraid/bootstrap.version` in the repo;
`$APP/.bootstrap.version` on disk. If missing or different, refresh
static `local.d` files and `*.template`s, then re-render secret files.
Never overwrite `accounts.yml` or the protected secrets file. Stage and
validate every tracked source before activating a refresh.

**Secret substitution.** Do not expand passwords on `sed` argv. `awk`
reads the password file (`-v pfile=`) and substitutes
`${RSPAMD_PASSWORD}` / `${REDIS_PASSWORD}`.

**Permissions.** Rendered `worker-controller.inc`,
`rspamd/local.d/redis.conf`, and the Redis server config are appdata
owner/group mode `0640`. Compose/Unraid maps that appdata group into rspamd
and runs Redis as uid 999 with the appdata primary group. Docker
`userns-remap` and rootless mode are rejected until a deployment-specific
shifted-ID contract is available; do not fall back to world-readable files.

---

## 4. Env vs YAML (locked)

`DEFAULT_JUNK_RETENTION_DAYS` / `DEFAULT_TRAINED_RETENTION_DAYS` apply
**only when that key is absent from YAML `defaults:`**. An explicit
`defaults.junk_retention_days: 30` wins. Unraid form still fills the
gap when YAML omits the key. Per-account keys already win via merge.

---

## 5. Images and secrets path (locked)

| Image | Pin |
|---|---|
| `rspamd/rspamd` | `4.1.3` |
| `mvance/unbound` | `1.22.0` |
| `python` (Dockerfile) | `3.14.7-slim-bookworm` |
| filter / redis | unchanged (`:latest` / `8-alpine`) |

The ByteLord Compose file keeps
`/opt/bytelord/secrets/imap-spamfilter.env`; the generic root Compose file
uses `SPAMFILTER_SECRETS` and defaults to the protected Unraid appdata path.
File mode is `600`. `.env` next to Compose remains optional/dev-only.

---

## 6. Redis

Keep `maxmemory-policy noeviction`, 1 GB, Bayes `expire = 0`. README:
monitor Redis memory; raise `maxmemory` rather than evict. A full Redis
fails **writes** (learns); it does not silently drop tokens.

---

## 7. Tests

1. YAML `defaults.junk_retention_days: 30` + env `10` → account gets 30.
2. No YAML key + env `10` → account gets 10.
3. Unset env + no YAML key → builtin 10.

---

## 8. Acceptance

`cd filter && python -m pytest -q` passes. `ps` of a running bootstrap
does not show generated passwords. README Linux install uses
`--env-file /opt/bytelord/secrets/imap-spamfilter.env`.
