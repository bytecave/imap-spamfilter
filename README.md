# imap-spamfilter

Docker-based IMAP spam filter for any IMAP server that supports IDLE.
Designed to run 24/7 on Unraid (templates included) or any Linux host with
Docker. Per-account modes, move-based Bayes training, never deletes mail.
Multi-arch image (`linux/amd64`, `linux/arm64`).

## Architecture

Four containers on a shared `spamnet` Docker network:

| Container          | Image                  | Role |
| ------------------ | ---------------------- | ---- |
| spamfilter-redis   | `redis:8-alpine`       | Persists rspamd Bayes tokens, fuzzy hashes, neural weights (AOF + RDB). |
| spamfilter-unbound | `mvance/unbound:latest`| Local recursive DNS. Keeps DNSBL lookups out of shared-resolver quotas. |
| spamfilter-rspamd  | `rspamd/rspamd:latest` | Scores messages: Bayes / fuzzy / neural / RBL. No autolearn. |
| spamfilter         | this repo (custom)     | Python service. One thread per account, IDLE on Inbox, polls Junk, scores, moves, learns. |

Per-account operating modes (set in `accounts.yml`, promoted manually):
- **shadow**  - scan + log only, no mailbox writes
- **flag**    - shadow + sets `\Flagged` on suspect mail
- **move**    - flag + after `move_grace_seconds`, MOVEs to Junk

Move-based training (no special folders needed in daily use):
- Inbox -> Junk = learn as spam (after `learn_grace_seconds`, default 300s)
- Junk -> Inbox = learn as ham  (after `learn_grace_seconds`)
- IMAP keyword `$Junk` / `$NotJunk` skips the grace window

Folder-based training (bootstrap and bulk corrections):
- Drag suspected spam to `Junk/Train-Spam` -> filter learns, moves to `Junk/Trained-Spam`
- `Junk/Trained-Spam` is swept to Trash after `trained_retention_days` (default 7)

Hard rules:
- **Never deletes.** Only IMAP MOVE. Trash retention is the mail provider's job.
- **Fails closed.** rspamd unreachable / parse error / folder missing => message stays put.
- **No autolearn.** Bayes only learns from explicit user moves or the Train-Spam folder.

---

## Folder discovery and naming

The filter cares about five folders per account: **Inbox**, **Junk**, **Trash**,
**Junk/Train-Spam**, **Junk/Trained-Spam**. Each can be configured per account,
but day one most users don't need to touch them.

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
```

Controlled by `auto_special_folders` (default `true`). Set it to `false`
in `accounts.yml` if you want the filter to use the literal `junk:` /
`trash:` values you configured.

### Auto-create

`Junk`, `Trash`, `Junk/Train-Spam`, and `Junk/Trained-Spam` are created
automatically on first connect if missing. `INBOX` is required by the IMAP
RFC and must already exist.

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

`spam_train` and `trained_spam` follow the (possibly auto-detected) `junk`
parent automatically; you only need to override them if you want them
somewhere outside the Junk subtree.

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
- Creates the `/mnt/user/appdata/spamfilter/{redis,state,rspamd/data,rspamd/local.d}` layout
- Downloads the rspamd `local.d/*` configs from this repo (only if missing)
- Seeds `accounts.yml` from `accounts.yml.example` (only if missing)
- Generates a random rspamd controller password into `state/controller.password` (only if missing)

Set the schedule to **"At First Array Start Only"** and click **Run Script**
once to bootstrap immediately. It'll re-run on every array start, so the
network/layout are recreated automatically after a USB reformat or migration.

If you'd rather not use User Scripts, run the same script over SSH:

```bash
curl -fsSL https://raw.githubusercontent.com/marcelverdult/imap-spamfilter/main/unraid/bootstrap.sh | bash
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
   10 / 7), install

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

Browse the rspamd web UI on `http://<unraid-ip>:11334`. Login password is
the auto-generated value:

```bash
cat /mnt/user/appdata/spamfilter/state/controller.password
```

### 4. Bootstrap training (optional but recommended)

Bayes is roughly useless until ~200 spam and ~200 ham are learned. Two
ways to feed it in bulk:

**a) Drop into the auto-created training folders (easiest, no CLI)**

The filter creates four folders on each account:

- `Train-Spam`   — drop **spam** here
- `Trained-Spam` — filter moves messages here after learning
- `Train-Ham`    — drop (or **copy**) **known-good** mail here
- `Trained-Ham`  — filter moves messages here after learning

Bulk-train ham without losing your originals: in your mail client, select
a known-good folder (e.g. `Archive/Family`) and **copy** to `Train-Ham`.
The filter learns each copy as ham, moves them to `Trained-Ham`, and
retention sweeps them to Trash after `trained_retention_days`. Originals
in your folders are untouched.

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

---

## Alternative install: any Linux host with Docker (no Unraid)

The published `ghcr.io/marcelverdult/imap-spamfilter` image is multi-arch
(`linux/amd64` and `linux/arm64`), so the same stack runs on Synology,
generic Linux servers, Raspberry Pi 4/5, ARM mini-PCs, etc.

```bash
git clone https://github.com/marcelverdult/imap-spamfilter
cd imap-spamfilter

# pick a host path you want appdata under, and edit docker-compose.yml
# to use it instead of /mnt/user/appdata/spamfilter (sed works fine):
APP=/srv/spamfilter
sed -i "s|/mnt/user/appdata/spamfilter|$APP|g" docker-compose.yml

mkdir -p $APP/{redis,state,rspamd/data,rspamd/local.d}
cp rspamd/local.d/* $APP/rspamd/local.d/
cp accounts.yml.example $APP/accounts.yml
chmod 600 $APP/accounts.yml
# edit $APP/accounts.yml + the fuzzy_check.conf encryption key

cp .env.example .env
# edit .env: set RSPAMD_PASSWORD

docker compose pull        # use the prebuilt ghcr image
docker compose up -d
docker compose logs -f spamfilter
```

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
| `imap_port` | `993` | port |
| `ssl` | `true` | `false` = use port 143 with STARTTLS instead |

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
| `mode` | `shadow` | `shadow` \| `flag` \| `move` |
| `threshold` | `8.0` | rspamd score >= this counts as spam |
| `min_threshold_allowed` | `5.0` | startup refuses to run if `threshold` is below this |
| `reject_score_above` | `100.0` | scores outside `±this` are treated as failed scan |

### Timing (all seconds)

| Key | Default | Notes |
| --- | --- | --- |
| `move_grace_seconds` | `60` | delay between flag and move (mode=move) |
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

### Retention

| Key | Default | Notes |
| --- | --- | --- |
| `junk_retention_days` | `10` | Junk -> Trash after N days, `0` disables |
| `trained_retention_days` | `7` | Trained-Spam **and** Trained-Ham -> Trash after N days |
| `learn_from_moves` | `true` | set `false` to disable all learning (scan-only) |

The `DEFAULT_JUNK_RETENTION_DAYS` and `DEFAULT_TRAINED_RETENTION_DAYS`
environment variables on the filter container override `defaults:` for
those two keys (useful for the Unraid template form).

### Bayes identity (sharing or isolating training across accounts)

rspamd's Bayes classifier in this project runs with `users_enabled = true`,
which means tokens are stored per-recipient. By default each IMAP account
in `accounts.yml` trains its own Bayes namespace keyed by its `user`.

| Key | Default | Notes |
| --- | --- | --- |
| `bayes_user` | unset | rspamd `User` header used for both scan and learn; overrides the per-recipient default |

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
├── state/
│   ├── spamfilter.db           # SQLite audit log + state
│   └── heartbeat               # epoch updated each loop (healthcheck source)
└── rspamd/
    ├── local.d/                # rspamd configs (you populate from this repo)
    └── data/                   # rspamd-managed caches
```

Back up `redis/`, `state/`, and `accounts.yml`. Skip `rspamd/data/` (rebuilds
itself). Unraid's built-in **CA Backup** plugin pointed at the appdata path is
sufficient.

---

## Operational queries

The SQLite DB is WAL mode; safe to query while the filter is running.

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

## Recovering from safe-mode

If an account hits a rate limit or sanity check, scanning continues but
moving/learning halts and a row is inserted into the `safe_mode` table.

```sql
SELECT account, scope, datetime(entered_at, 'unixepoch', 'localtime'), reason
FROM safe_mode;

-- clear after investigating
DELETE FROM safe_mode WHERE account='your_name';
-- or to clear all: DELETE FROM safe_mode;
```

Safe-mode is sticky on purpose; it requires a human to evaluate whether the
trigger was a real problem or a benign spike.

---

## Known limitations

- **No allowlist.** Intentional. rspamd's DKIM/SPF symbols already give
  negative score to aligned mail. Fix misclassifications by training, not
  by allowlisting.
- **No web UI for accounts.** All inspection via SQLite or the rspamd UI.
- **IDLE re-issued every `idle_timeout` (default 1500s).** Lower it if your
  server drops idle connections faster.
- **No multi-host coordination.** Don't run two filter instances against
  the same mailbox.

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
│   └── bootstrap_train.py
├── rspamd/local.d/               # drop into /mnt/user/appdata/spamfilter/rspamd/local.d/
└── unraid/                       # Unraid Docker templates (one per container)
    ├── redis.xml
    ├── unbound.xml
    ├── rspamd.xml
    └── spamfilter.xml
```

`.env`, `accounts.yml`, `state/`, `redis/`, `rspamd/data/`, and the rendered
`worker-controller.inc` are gitignored. Nothing in version control contains
secrets.
