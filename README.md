# imap-spamfilter

Docker-based IMAP spam filter for any IMAP server that supports IDLE.
Designed to run 24/7 on Unraid (templates included) or any Linux host with
Docker. Per-account modes, move-based Bayes training, never deletes mail.
Optional read-only web dashboard with per-user access levels.
Multi-arch image (`linux/amd64`, `linux/arm64`).

## Architecture

Four containers on a shared `spamnet` Docker network:

| Container          | Image                  | Role |
| ------------------ | ---------------------- | ---- |
| spamfilter-redis   | `redis:8-alpine`       | Persists rspamd Bayes tokens, fuzzy hashes, neural weights (AOF + RDB). |
| spamfilter-unbound | `mvance/unbound:1.22.0`| Local recursive DNS. Keeps DNSBL lookups out of shared-resolver quotas. |
| spamfilter-rspamd  | `rspamd/rspamd:4.1.3` | Scores messages: Bayes / fuzzy / neural / RBL. No autolearn. |
| spamfilter         | this repo (custom)     | Python service. One thread per account, IDLE on Inbox, polls Junk, scores, moves, learns. |

Per-account operating modes (set in `accounts.yml`, promoted manually):
- **shadow**  - scan + log only. No writes to Inbox, Junk, or Trash.
  Train-* folders may still be created and drained so Bayes can be
  bootstrapped during evaluation.
- **flag**    - shadow + sets `\Flagged` on suspect Inbox mail; retention on
- **move**    - flag + after `move_grace_seconds`, MOVEs Inbox → Junk

Move-based training (no special folders needed in daily use):
- Inbox -> Junk = learn as spam (after `learn_grace_seconds`, default 300s)
- Junk -> Inbox = learn as ham  (after `learn_grace_seconds`)
- IMAP keyword `$Junk` / `$NotJunk` skips the grace window

Folder-based training (bootstrap and bulk corrections):
- **Move** spam to `Junk/Train-Spam` -> filter learns, moves to `Junk/Trained-Spam`
- **Copy** (never move) known-good mail to `Junk/Train-Ham` -> filter learns,
  moves to `Junk/Trained-Ham`. The copy is destroyed by retention; the
  original in your sorted folder stays untouched. **Moving** ham here means
  the only copy will eventually end up in Trash.
- Both `Junk/Trained-*` folders are swept to Trash after `trained_retention_days` (default 7)

Hard rules:
- **Never deletes.** Only IMAP MOVE. Trash retention is the mail provider's job.
- **Fails closed.** rspamd unreachable / parse error / folder missing => message stays put.
- **No autolearn.** Bayes only learns from explicit user moves or the Train-Spam / Train-Ham folders.

---

## Folder discovery and naming

The filter cares about seven folders per account: **Inbox**, **Junk**,
**Trash**, **Junk/Train-Spam**, **Junk/Trained-Spam**, **Junk/Train-Ham**,
**Junk/Trained-Ham**. Day one most users don't need to touch any of them.

### Hierarchy delimiter (auto)

On connect the filter runs `LIST "" "*"` and reads the server's hierarchy
delimiter (typically `/` or `.`). Folder names in `accounts.yml` use `/` and
get rewritten at runtime: `Junk/Train-Spam` becomes `Junk.Train-Spam` on a
Dovecot server using `.`. The detected delimiter is logged once at connect:

```
connected, delimiter='/', mode=shadow
```

### Junk / Trash auto-detection (RFC 6154)

If your IMAP server advertises folders with RFC 6154 **SPECIAL-USE** attribute
flags (most modern servers do), the filter uses those names regardless of
what your mail client decided to call them locally. So if Apple Mail picked
`Spam` as the junk folder (and the server tagged it `\Junk`), the filter
follows the same folder automatically. No config edit required.

Logged at connect when override happens:
```
auto-detected junk folder via SPECIAL-USE: Spam (was Junk)
auto-detected trash folder via SPECIAL-USE: Bin  (was Trash)
remapped spam_train:   Junk/Train-Spam   -> Spam/Train-Spam
remapped trained_spam: Junk/Trained-Spam -> Spam/Trained-Spam
remapped ham_train:    Junk/Train-Ham    -> Spam/Train-Ham
remapped trained_ham:  Junk/Trained-Ham  -> Spam/Trained-Ham
```

Controlled by `auto_special_folders` (default `true`). Set it to `false`
in `accounts.yml` if you want the filter to use the literal `junk:` /
`trash:` values you configured.

### Auto-create policy

The filter only auto-creates folders it **owns**:

- `Junk/Train-Spam`, `Junk/Trained-Spam`, `Junk/Train-Ham`,
  `Junk/Trained-Ham` (under the server-detected junk parent)

It **refuses** to create the core user folders:

- `INBOX` (required by the IMAP RFC, must already exist)
- `Junk` and `Trash`

If `Junk` or `Trash` are missing after SPECIAL-USE detection, the filter
aborts with a clear error rather than silently creating duplicate
hierarchies in your mailbox (this is how an account ends up with two
parallel `Junk/` and `Spam/` trees and the filter trains under the wrong
one). Either enable SPECIAL-USE on your IMAP server, or set explicit
`junk:` / `trash:` names in `accounts.yml` that match folders that
already exist.

### Manual override

To pin specific folder names (e.g. you want the filter to use a folder
named `Spam-quarantine` rather than whatever the server thinks is `\Junk`):

```yaml
accounts:
  - name: your_name
    imap_host: ...
    user: ...
    password: "..."
    auto_special_folders: false
    junk: Spam-quarantine
    trash: Bin
```

`spam_train`, `trained_spam`, `ham_train`, and `trained_ham` follow the
(possibly auto-detected) `junk` parent automatically; you only need to
override them if you want them somewhere outside the Junk subtree.

---

## Install on Unraid (recommended path)

Each container installs as a normal Unraid Docker app. No Compose Manager
required. All four containers run on a shared user-defined Docker network
called `spamnet` so they can resolve each other by name.

### 0. Bootstrap (one shot)

Install **User Scripts** from Community Apps if you don't already have it.
Settings -> User Scripts -> Add New Script, name it `spamfilter-bootstrap`,
paste in the contents of
[`unraid/bootstrap.sh`](unraid/bootstrap.sh):

The script is idempotent and does all of the following:

- Creates the user-defined `spamnet` Docker network
- Creates the `/mnt/user/appdata/spamfilter/{redis,redis-config,state,rspamd/data,rspamd/local.d}` layout
  (the `redis/` and `rspamd/data/` dirs are owned by the images' internal uids, mode 750)
- Copies rspamd `local.d/*` from the git checkout when present; otherwise fetches a
  **pinned commit** (`SPAMFILTER_REF`, never floating `/main`)
- Seeds `accounts.yml` from `accounts.yml.example` (only if missing)
- Generates random passwords into `state/controller.password` (rspamd controller)
  and `state/redis.password` (Redis auth), only if missing
- Renders `worker-controller.inc`, the rspamd `redis.conf` client config, and the
  Redis server config into `redis-config/redis.conf` with those passwords substituted
  in (awk reads the password file; secrets never appear on `ps`)
- Writes `$APP/.bootstrap.version` from `unraid/bootstrap.version`. A missing or
  different stamp refreshes static `local.d` files and templates, then re-renders
  secret files. It never overwrites `accounts.yml` or `state/*.password`.

Set the schedule to **"At First Array Start Only"** and click **Run Script**
once to bootstrap immediately. It'll re-run on every array start, so the
network/layout are recreated automatically after a USB reformat or migration.
After pulling template/config fixes, re-run the script so the version stamp
can refresh `local.d` (your accounts and generated passwords stay put).

If you'd rather not use User Scripts, clone the repo and run the script over SSH
(preferred: copies configs from disk). Do not pipe bootstrap.sh from
`.../main` — that ref floats. Without a clone, curl a **commit SHA**:

```bash
# From a checkout:
bash unraid/bootstrap.sh

# No checkout: pin the script URL to a commit (same idea as SPAMFILTER_REF).
# curl -fsSL https://raw.githubusercontent.com/marcelverdult/imap-spamfilter/<commit>/unraid/bootstrap.sh | bash
```

### 1. Edit `accounts.yml`

```bash
nano /mnt/user/appdata/spamfilter/accounts.yml
```

Set `imap_host`, fill in each account's `user` / `password`, leave
`mode: shadow` for the first week. This is the only file you have to
edit by hand.

### 2. Install the four templates

In the Unraid web UI (Docker tab -> Add Container -> Template -> "Add" a
local template), import each XML from this repo's `unraid/` directory:

1. `unraid/spamfilter-redis.xml`   -> install (no prompts beyond the data path)
2. `unraid/spamfilter-unbound.xml` -> install
3. `unraid/spamfilter-rspamd.xml`  -> install (controller password is read from
   the bootstrap-generated `state/controller.password` file)
4. `unraid/spamfilter.xml`         -> set `DEFAULT_JUNK_RETENTION_DAYS` and
   `DEFAULT_TRAINED_RETENTION_DAYS` if you want non-defaults (defaults
   10 / 7) **and** `accounts.yml` `defaults:` omits those keys. Explicit YAML
   wins. Then install.

Each template defaults its paths under `/mnt/user/appdata/spamfilter/<service>`,
matches typical Unraid conventions, and references `Network=spamnet`.

> **Tip.** If you'd rather get the templates without cloning the repo,
> point Unraid's "Template repositories" setting (Docker tab -> Advanced
> view) at this GitHub repo URL. The XMLs in `unraid/` will then show up
> in the standard "Add Container" template picker.

### 3. First run

After `spamfilter` starts, watch the logs:

```bash
docker logs -f spamfilter
```

Expect:

```
[main]      loaded 1 account(s): your_name
[your_name] connecting to imap.your-mail-provider.example:993 as you@your-domain.example
[your_name] connected, delimiter='.', mode=shadow
```

The rspamd controller (port 11334) is **not** published to the host — the
stack deliberately keeps it on the internal `spamnet` network only. To
reach rspamd's own web UI, either add an `11334:11334` port mapping to the
rspamd container yourself, or use the read-only dashboard (see below). The
controller password, if you need it, is the auto-generated value:

```bash
cat /mnt/user/appdata/spamfilter/state/controller.password
```

### 4. Bootstrap training (optional but recommended)

Bayes is roughly useless until ~200 spam and ~200 ham are learned. Two
ways to feed it in bulk:

**a) Drop into the auto-created training folders (easiest, no CLI)**

The filter creates four folders on each account:

- `Train-Spam`   — **move** spam here (originals are spam, OK to lose)
- `Trained-Spam` — filter archives here after learning; retention -> Trash
- `Train-Ham`    — **copy only** known-good mail here (never move)
- `Trained-Ham`  — filter archives here after learning; retention -> Trash

**Why copy for ham:** the copy in `Train-Ham` is destroyed by retention.
If you **move** a legitimate mail into `Train-Ham` you lose your only
copy. Bulk-train ham by selecting a known-good folder in your mail
client (e.g. `Archive/Family`) and **Copy** to `Train-Ham` - the
originals in your folders stay untouched.

**b) bootstrap_train.py CLI (faster for one-off bulk runs)**

```bash
docker exec -it spamfilter python bootstrap_train.py your_name Train-Spam spam --dry-run
docker exec -it spamfilter python bootstrap_train.py your_name Train-Spam spam --move-to Trained-Spam
docker exec -it spamfilter python bootstrap_train.py your_name Train-Ham  ham  --move-to Trained-Ham
```

### 5. Mode promotion

After ~1 week in `shadow`:

```bash
vi /mnt/user/appdata/spamfilter/accounts.yml   # change mode: shadow -> flag
docker restart spamfilter
```

After another week, promote to `move`. Promote each family member
independently. Modify the file by hand any time - changes take effect on
container restart.

Retention (Junk → Trash, and Trained-* → Trash) starts when you leave
`shadow`. A mailbox that has sat in shadow for weeks may have old Junk;
the first `flag`/`move` retention pass will honour `junk_retention_days`
(default 10). If that is too aggressive, set `junk_retention_days: 0`
(or a larger number) **before** promoting.

---

## Alternative install: any Linux host with Docker (no Unraid)

The published `ghcr.io/marcelverdult/imap-spamfilter` image is multi-arch
(`linux/amd64` and `linux/arm64`), so the same stack runs on Synology,
generic Linux servers, Raspberry Pi 4/5, ARM mini-PCs, etc.

```bash
git clone https://github.com/marcelverdult/imap-spamfilter
cd imap-spamfilter

# Pick a host path for appdata and point docker-compose.yml at it:
export SPAMFILTER_APP=/srv/spamfilter
sed -i "s|/mnt/user/appdata/spamfilter|$SPAMFILTER_APP|g" docker-compose.yml

# Run the bootstrap: it creates the directory layout, copies (or fetches
# a pinned commit of) the rspamd + redis configs, and generates the
# rspamd controller and Redis passwords. SPAMFILTER_APP tells it where.
bash unraid/bootstrap.sh

# Edit the seeded account list (the only file you must touch by hand):
nano $SPAMFILTER_APP/accounts.yml

# Production secrets live outside the git tree (mode 600). Do not rely
# on a compose-adjacent .env (gitignored; easy to recreate empty).
sudo install -d -m 700 /opt/bytelord/secrets
sudo install -m 600 /dev/null /opt/bytelord/secrets/imap-spamfilter.env
# Optional: put RSPAMD_PASSWORD=... there only if you are not using
# bootstrap's state/controller.password. Dashboard vars go here too.

docker compose pull        # use the prebuilt ghcr image
docker compose --env-file /opt/bytelord/secrets/imap-spamfilter.env up -d
docker compose --env-file /opt/bytelord/secrets/imap-spamfilter.env logs -f spamfilter
```

`bootstrap.sh` is the single source of the rendered configs — the
rspamd `worker-controller.inc`, the rspamd Redis client config, and
the Redis server config — so the compose stack just mounts what it
produced, exactly like the Unraid path. Re-run it after pulling config
changes from the repo; a new `unraid/bootstrap.version` refreshes
templates without touching `accounts.yml` or generated passwords.

Leave `RSPAMD_PASSWORD` unset in the env file and the filter reads the
bootstrap-generated `state/controller.password`. A compose-adjacent
`.env` is fine for a laptop/dev clone only.

The compose file matches the Unraid layout one-for-one, so backups,
docs, and the SQLite audit queries all apply the same way. Pick one
install path; don't run both against the same mailbox.

---

## Verify before deploying

Items the spec did not pin. Confirm before trusting the filter in `move` mode.

1. **IMAP hostname** - get from your mail provider's account/admin panel.
   Set as `imap_host:` in `accounts.yml`. Verify by opening port 993 with
   `openssl s_client -connect host:993` if unsure.
2. **IMAP folder hierarchy delimiter** - auto-detected on connect, logged
   as `connected, delimiter='.', mode=shadow`. Folder names in config use
   `/`; filter rewrites to whatever the server actually uses.
3. **`$Junk` / `$NotJunk` keyword spelling** - RFC 5788 names. Apple Mail
   and most modern clients set them literally. If your client uses a vendor
   variant, edit `JUNK_KEYWORD` / `NOTJUNK_KEYWORD` in `filter/filter.py`.
4. **Filter container image** - prebuilt and pushed to
   `ghcr.io/marcelverdult/imap-spamfilter:latest` by the GitHub Actions
   workflow in `.github/workflows/build.yml` on every push to `main` and
   every `v*` tag. After the first push you must flip the GHCR package
   visibility to **public** (GitHub -> your profile -> Packages ->
   `imap-spamfilter` -> Package settings -> Change visibility -> Public),
   otherwise Unraid will fail to pull with `manifest unknown`.
   If you'd rather build locally:
   ```bash
   docker build -t imap-spamfilter:local ./filter
   ```
   then edit the Unraid template's `Repository` field.

---

## Configuration reference

Every field below is optional unless marked **required**. Per-account values
override `defaults:` values; both override built-in defaults from `filter.py`.

### Required per account

| Key | Example | Notes |
| --- | --- | --- |
| `name` | `marcel` | label used in logs and SQLite, must be unique |
| `imap_host` | `imap.example.de` | hostname only, no scheme |
| `user` | `you@example.de` | login username (usually the full address) |
| `password` | `"..."` | quote to keep YAML happy with special chars |

### Connection

| Key | Default | Notes |
| --- | --- | --- |
| `imap_port` | `993` | port (143 typical for `starttls`) |
| `tls_mode` | `implicit` | `implicit` (IMAPS) \| `starttls` (plain then STARTTLS before LOGIN) \| `none` (plaintext; loopback only, or `allow_insecure_tls: true`) |
| `allow_insecure_tls` | `false` | required with `tls_mode: none` unless `imap_host` is loopback |
| `ssl` | — | **deprecated.** `true` → `implicit`, `false` → `starttls`. Do not use; it never meant plaintext. |

### Folder names

| Key | Default | Notes |
| --- | --- | --- |
| `inbox` | `INBOX` | RFC-mandated; do not change |
| `junk` | `Junk` | auto-detected via RFC 6154 if server advertises `\Junk` |
| `trash` | `Trash` | auto-detected via `\Trash` |
| `spam_train` | `Junk/Train-Spam` | drop spam here for the filter to learn |
| `trained_spam` | `Junk/Trained-Spam` | post-learn archive (auto-trashed by retention) |
| `ham_train` | `Junk/Train-Ham` | drop (or **copy**) known-good mail here for ham training |
| `trained_ham` | `Junk/Trained-Ham` | post-learn archive for ham (auto-trashed by retention) |
| `auto_special_folders` | `true` | set `false` to use literal `junk`/`trash` names |

All four train/trained folders live under the server's junk parent and are
auto-relocated together when SPECIAL-USE remaps the junk name (e.g. when
the server actually flags `Spam` as `\Junk`, the four become
`Spam/Train-Spam`, `Spam/Train-Ham`, etc.). The filter refuses to create
`Junk` / `Trash` themselves to avoid duplicate junk hierarchies in the
user's mailbox.

### Mode and scoring

| Key | Default | Notes |
| --- | --- | --- |
| `mode` | `shadow` | `shadow` (no Inbox/Junk/Trash writes; Train-* drain allowed) \| `flag` \| `move` |
| `threshold` | `8.0` | rspamd score >= this counts as spam |
| `min_threshold_allowed` | `5.0` | startup refuses to run if `threshold` is below this |
| `reject_score_above` | `100.0` | scores outside `±this` are treated as failed scan |

### Timing (all seconds)

| Key | Default | Notes |
| --- | --- | --- |
| `move_grace_seconds` | `60` | delay between flag and move (mode=move); `0` = move instantly |
| `learn_grace_seconds` | `300` | undo window before any Bayes update |
| `idle_timeout` | `1500` | IMAP IDLE re-issue interval (must be < 30 min) |
| `poll_interval` | `600` | fallback poll when IDLE not supported |
| `junk_poll_interval` | `120` | how often to scan Junk for user moves |
| `retention_check_interval` | `3600` | how often retention sweeps run |

### Rate limits

| Key | Default | Notes |
| --- | --- | --- |
| `max_moves_per_hour` | `30` | breach triggers safe-mode for the account |
| `max_learns_per_hour` | `50` | breach triggers learning-only safe-mode |
| `max_train_per_run` | `100` | cap per `drain_train_spam` batch |
| `safe_mode_unseen_cap` | `500` | sticky safe-mode if Inbox UNSEEN exceeds this; raise for accounts that normally keep many unread |

### Retention

| Key | Default | Notes |
| --- | --- | --- |
| `junk_retention_days` | `10` | Junk -> Trash after N days, `0` disables |
| `trained_retention_days` | `7` | Trained-Spam **and** Trained-Ham -> Trash after N days |
| `learn_from_moves` | `true` | set `false` to disable all learning (scan-only) |

The `DEFAULT_JUNK_RETENTION_DAYS` and `DEFAULT_TRAINED_RETENTION_DAYS`
environment variables on the filter container apply **only when that key
is absent from YAML `defaults:`**. An explicit
`defaults.junk_retention_days: 30` wins over a template field of `10`.
Per-account keys still win via the usual merge. The Unraid form fills
the gap when YAML omits the key.

### Bayes identity (sharing or isolating training across accounts)

rspamd's Bayes classifier in this project runs with `users_enabled = true`,
which means tokens are stored per-recipient. By default each IMAP account
in `accounts.yml` trains its own Bayes namespace keyed by its `user`.

| Key | Default | Notes |
| --- | --- | --- |
| `bayes_user` | unset | HTTP `Rcpt` on scan (and `Delivered-To` on learn); overrides the per-recipient default |

Use `bayes_user` to pool training across several of your own mailboxes
while leaving other users (e.g. family members) isolated. Set the same
`bayes_user` value on every account that should share data. Accounts that
omit the field stay isolated under their own IMAP user.

```yaml
accounts:
  - name: marcel_main
    user: marcel@verdult.de
    password: "..."
    bayes_user: marcel-pool        # shared
  - name: marcel_work
    user: work@verdult.de
    password: "..."
    bayes_user: marcel-pool        # shared (same value)
  - name: family_member
    user: kid@verdult.de
    password: "..."
    # no bayes_user -> isolated, keyed by kid@verdult.de
```

Switching an existing account from per-recipient to a `bayes_user` value
(or vice versa) starts a fresh Bayes namespace. The prior tokens stay in
Redis under the old key but are no longer consulted. Either re-train under
the new identity (drop mail back into Train-Spam) or migrate the keys in
Redis manually.

---

## Persistent data

Everything stateful lives under `/mnt/user/appdata/spamfilter/`:

```
/mnt/user/appdata/spamfilter/
├── accounts.yml                # account list and per-account overrides (SECRETS)
├── redis/                      # Bayes corpus, fuzzy hashes, neural weights
├── redis-config/redis.conf     # rendered Redis server config (bootstrap.sh)
├── state/
│   ├── spamfilter.db           # SQLite audit log + state
│   ├── heartbeat               # epoch updated each loop (healthcheck source)
│   ├── controller.password     # generated rspamd controller password
│   ├── redis.password          # generated Redis password
│   ├── dashboard_secret        # generated dashboard session secret
│   └── dashboard_users         # dashboard logins (present once the dashboard is used)
└── rspamd/
    ├── local.d/                # rspamd configs (downloaded + rendered by bootstrap.sh)
    └── data/                   # rspamd-managed caches
```

Back up `redis/`, `state/`, and `accounts.yml`. Skip `rspamd/data/` and
`redis-config/` (both regenerate — the latter is re-rendered by
`bootstrap.sh` from `state/redis.password`). Unraid's built-in **CA
Backup** plugin pointed at the appdata path is sufficient.

Redis is capped at 1 GB with `maxmemory-policy noeviction` and Bayes
`expire = 0`. That is intentional: LRU would silently drop tokens and
degrade accuracy. Monitor Redis memory (`INFO memory`); if it approaches
the cap, raise `maxmemory` in `redis/redis.conf.template` and re-run
bootstrap. A full Redis **fails writes** (learns), it does not evict.

---

## Web dashboard (optional)

A small read-only Flask dashboard is available for at-a-glance stats:
a health banner, filter KPIs, a 14-day scan trend, rspamd Bayes
progress, recent scans/learns, and per-account activity. Responsive,
dark-mode aware. **Off by default.** No actions, no buttons —
read-only.

The container always listens internally on **port 8080**; pick any
free host port in your orchestrator's port mapping.

### Login

Access is gated by a real login form with a server-side session
(no more browser-cached basic auth). Add users with the bundled
helper inside the container:

```bash
docker exec -it spamfilter python dashboard.py
# prompts for a username + password, writes state/dashboard_users
```

It adds (or updates) the user in `state/dashboard_users` — one
`username:hash:scope` line per user, passwords pbkdf2-hashed, `#`
comments allowed. The dashboard re-reads that file on every login, so
adding or changing a user takes effect **without a restart**. You can
edit the file by hand too.

**Access levels.** The helper also asks for a scope:

- `admin` — sees everything: all accounts plus the system-wide rspamd
  lifetime totals and Bayes stats.
- one or more **account names** (the `name:` values from
  `accounts.yml`) — a restricted user who sees only those accounts'
  scans, learns, events and per-account stats, and not the global
  rspamd section. Handy for letting a household member watch the
  filter on their own mailbox. Usernames are free-form, so an email
  address works fine as the login name.

The helper lists the account names from `accounts.yml` and rejects an
unknown one, so a typo cannot silently bind a user to nothing. A line
with no scope field is **ignored** (fail closed). Write `:admin`
explicitly. The helper always includes a scope.

Two env-var alternatives also work, if you prefer config over a file:
`DASHBOARD_USERS` (comma-separated `username:hash:scope` entries) and
the legacy single-user `DASHBOARD_USER` + `DASHBOARD_PASSWORD`
(plaintext, admin). All three sources merge. Prefer the hashed-users
file over the legacy plaintext env pair.

The session signing secret is generated once into
`state/dashboard_secret` (mode 600) and reused across restarts.
Sessions expire after 8 hours idle or 24 hours absolute. Logout is a
POST (the nav button). GET `/logout` does not end the session.

The dashboard starts once at least one user exists (file or env). On
Unraid set the host port in the "Dashboard port" mapping and Apply
(LAN access). On a VPS publish loopback only, for example
`127.0.0.1:8080:8080` in Compose, and put a reverse proxy in front if
you need remote access. Do not publish `8080:8080` on a public
interface.

Open `http://<host>:<port>/` (or `http://127.0.0.1:<port>/` on a VPS).

There is no TLS in the dashboard itself — terminate HTTPS at a
reverse proxy if you expose it beyond your LAN. If the proxy forwards
HTTPS, set `DASHBOARD_COOKIE_SECURE=1` so the session cookie is
marked Secure.

Pages:

- `/`         health banner + filter KPIs (24h / 7d, spam-catch rate)
              + 14-day scan-per-day trend + rspamd lifetime totals
              (scanned, spam/ham counts, fuzzy hashes, connections,
              action breakdown) + Bayes learn progress bars with
              per-class status and learn-balance check + active
              safe-mode + recent learns
- `/messages` last 200 scored msgs with score-band filter
- `/learned`  last 300 learn / learn_failed / learn_giveup events
- `/events`   tail of the full events table
- `/accounts` per-account scan / learn / fail counts, total spam &
              ham learns, plus safe-mode

---

## Operational queries

The SQLite DB is WAL mode; safe to query while the filter is running.

Messages are identified by IMAP `(account, folder, uidvalidity, uid)`,
not by `Message-ID`. Duplicate Message-IDs are independent objects;
`Message-ID` is searchable metadata only.

```bash
sqlite3 /mnt/user/appdata/spamfilter/state/spamfilter.db
```

Useful queries:

```sql
-- 50 most recent events for an account
SELECT datetime(ts, 'unixepoch', 'localtime'), event, substr(message_id,1,40), detail
FROM events WHERE account='your_name' ORDER BY ts DESC LIMIT 50;

-- "I think I lost a mail" - search by subject across all folders
SELECT datetime(last_seen, 'unixepoch', 'localtime'),
       current_folder, our_score, our_action, learned_as, sender, subject
FROM messages WHERE account='your_name' AND subject LIKE '%invoice%';

-- per-account rate consumption in the last hour
SELECT account, action, COUNT(*) FROM rate_limit
WHERE ts >= strftime('%s','now','-1 hour') GROUP BY account, action;
```

---

## Safe-mode and rate limits

Two distinct mechanisms protect the account from runaway behaviour:

### Rate limits (soft refusal, self-recovering)

`max_moves_per_hour`, `max_learns_per_hour`, and `max_train_per_run`
cap the number of mailbox-modifying actions per hour. When a limit is
hit the filter logs a warning **once per minute**, refuses that action
for the rest of the rolling-hour window, then resumes automatically as
old entries roll out of the window. No DB state, no manual recovery.

### Safe-mode (sticky-ish, scoped)

The only sticky safe-mode is `scope="all"` triggered when Inbox UNSEEN
*above the scan bookmark* exceeds `safe_mode_unseen_cap` (default 500) -
a sanity check that something is wrong with the mailbox (mass-import,
server restored from backup, etc.). Historical unread mail below the
bookmark does not count. Scanning halts for that account. Raise the
per-account override in `accounts.yml` for inboxes that legitimately
keep a large unread backlog of *new* mail.

It **auto-exits** on the next `scan_inbox` pass once UNSEEN drops back
under the cap, so the typical "I marked everything read" recovery is
hands-off. To clear manually anyway:

```sql
SELECT account, scope, datetime(entered_at, 'unixepoch', 'localtime'), reason
FROM safe_mode;
DELETE FROM safe_mode WHERE account='your_name';
-- or to clear all: DELETE FROM safe_mode;
```

---

## Backups

Everything stateful lives under `/mnt/user/appdata/spamfilter/`. Nightly
backups of that whole tree preserve:

- SQLite state DB (filter's per-account messages, events, rate-limit,
  safe-mode, uidvalidity tables)
- Redis AOF + RDB (Bayes tokens — the actual training)
- rspamd `/var/lib/rspamd` cache (incl. neural-meta weights, which take
  days of confident decisions to rebuild from scratch)
- accounts.yml, the rspamd controller password, and the Redis password
  (`state/controller.password`, `state/redis.password`)

On Unraid, install the **Appdata Backup** Community App (by `KluthR`)
and schedule it nightly. Set "Stop container before backup" for all
four `spamfilter*` containers - downtime is ~30 s while the tar runs,
and the filter reconnects automatically via IDLE. Keep e.g. 14 daily
snapshots; expect 50-150 MB raw per snapshot, ~10-40 MB after zst
compression.

Restore is the reverse: stop the four containers, extract the tar over
`/mnt/user/appdata/spamfilter/`, start the containers.

---

## Known limitations

- **No allowlist.** Intentional. rspamd's DKIM/SPF symbols already give
  negative score to aligned mail. Fix misclassifications by training, not
  by allowlisting.
- **IMAP scoring has no SMTP client IP.** The filter does not send `Ip`
  or `Helo` to `/checkv2`. RBL `from` lookups, SPF, and DMARC envelope
  alignment are degraded; they rely on `Received:` chains in the message
  plus Bayes/fuzzy/neural. HTTP `From` is the message From (not the IMAP
  recipient). `Rcpt` remains the Bayes identity (`bayes_user` or the
  account user).
- **Dashboard is read-only.** It shows activity; it has no controls to
  move, learn, or change config. Inspect deeper via SQLite if needed.
- **IDLE re-issued every `idle_timeout` (default 1500s).** Lower it if your
  server drops idle connections faster.
- **No multi-host coordination.** Don't run two filter instances against
  the same mailbox.
- **Messages larger than 5 MiB are not fully downloaded.** They stay in
  place, are never auto-moved or scored, and are logged as
  `skipped_oversize`. Train-* oversize is moved to Trained-* unlearned
  so drain cannot loop.

---

## Repository contents

```
.
├── README.md
├── LICENSE
├── docker-compose.yml            # alt install path
├── .env.example
├── .gitignore
├── accounts.yml.example
├── filter/                       # custom Python service
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── filter.py
│   ├── dashboard.py
│   └── bootstrap_train.py
├── redis/                        # Redis server config template
├── rspamd/local.d/               # rspamd config templates + static configs
└── unraid/                       # bootstrap.sh + Unraid Docker templates
    ├── bootstrap.sh
    ├── bootstrap.version         # bump to refresh installed local.d/templates
    ├── spamfilter-redis.xml
    ├── spamfilter-unbound.xml
    ├── spamfilter-rspamd.xml
    └── spamfilter.xml
```

`.env`, `accounts.yml`, `state/`, `rspamd/data/`, the appdata `redis/` data
dir and `redis-config/`, and the rendered secret-bearing configs
(`worker-controller.inc`, `rspamd/local.d/redis.conf`) are gitignored. Only
the `*.template` files are tracked; nothing in version control contains
secrets.
