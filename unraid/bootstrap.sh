#!/bin/bash
# imap-spamfilter Unraid bootstrap.
# Idempotent: safe to run any number of times. Set as User Scripts entry
# scheduled "At First Array Start Only" so the appdata layout and the
# shared `spamnet` Docker network are always in place before the
# spamfilter-* containers start.
set -e

APP=/mnt/user/appdata/spamfilter
BASE=https://raw.githubusercontent.com/marcelverdult/imap-spamfilter/main
# Unraid container convention: appdata files belong to nobody:users.
APP_UID=99
APP_GID=100

# 1. Docker network shared by all four containers
docker network create spamnet 2>/dev/null || true

# 2. Directory layout under appdata.
# - All dirs owned by nobody:users (Unraid appdata convention).
# - redis/, rspamd/data/ kept world-writable (777) so the official
#   redis/rspamd images can write as their internal uids (999 / 102).
# - state/ stays 755 (only the filter writes, runs as 99:100).
mkdir -p "$APP"/{redis,state,rspamd/data,rspamd/local.d}
chown "$APP_UID:$APP_GID" "$APP" "$APP/redis" "$APP/state" \
                          "$APP/rspamd" "$APP/rspamd/data" "$APP/rspamd/local.d"
chmod 755 "$APP" "$APP/state" "$APP/rspamd" "$APP/rspamd/local.d"
chmod 777 "$APP/redis" "$APP/rspamd/data"

# 3. rspamd local.d configs (download only what's missing)
RSPAMD_FILES=(
  redis.conf
  classifier-bayes.conf
  worker-normal.inc
  worker-controller.inc.template
  options.inc
  actions.conf
  fuzzy_check.conf
  neural.conf
  rbl.conf
)
for f in "${RSPAMD_FILES[@]}"; do
  dst="$APP/rspamd/local.d/$f"
  if [ ! -f "$dst" ]; then
    echo "fetching rspamd/local.d/$f"
    curl -fsSL "$BASE/rspamd/local.d/$f" -o "$dst"
  fi
  chown "$APP_UID:$APP_GID" "$dst"
  chmod 644 "$dst"
done

# 4. accounts.yml seed (only if not present; user must edit afterwards)
if [ ! -f "$APP/accounts.yml" ]; then
  echo "seeding accounts.yml from accounts.yml.example"
  curl -fsSL "$BASE/accounts.yml.example" -o "$APP/accounts.yml"
  echo
  echo "  >>> EDIT $APP/accounts.yml before starting the spamfilter container"
  echo
fi
# 640 so the filter container (running as Unraid's nobody:users, uid
# 99 gid 100, matching $APP_UID/$APP_GID) can read accounts.yml via
# the bind mount, while world has no access. Appdata is already
# restricted at the share level on the host.
chown "$APP_UID:$APP_GID" "$APP/accounts.yml"
chmod 640 "$APP/accounts.yml"

# 5. rspamd controller password (random, persistent). Both rspamd and the
#    filter container read it from this file, so the user never sets it
#    in the Unraid template.
PW_FILE="$APP/state/controller.password"
if [ ! -f "$PW_FILE" ]; then
  echo "generating rspamd controller password"
  # Write to a temp file then rename so the password file is never
  # observable in a partially-written state, and append a trailing
  # newline so common tools (cat, less, while-read) handle it cleanly.
  ( umask 077 && {
      openssl rand -base64 48 | tr -d '\n'
      echo
    } > "$PW_FILE.tmp"
  )
  mv "$PW_FILE.tmp" "$PW_FILE"
fi
chown "$APP_UID:$APP_GID" "$PW_FILE"
chmod 640 "$PW_FILE"

# 6. Render worker-controller.inc from the .template now (host side),
#    so the rspamd container can mount local.d/ as read-only and start
#    with the official entrypoint - no cp/envsubst gymnastics.
TEMPLATE="$APP/rspamd/local.d/worker-controller.inc.template"
TARGET="$APP/rspamd/local.d/worker-controller.inc"
if [ -f "$TEMPLATE" ]; then
  PW="$(cat "$PW_FILE")"
  # sed-based substitution avoids depending on gettext/envsubst being on the host.
  sed "s|\${RSPAMD_PASSWORD}|${PW}|g" "$TEMPLATE" > "$TARGET"
  # 644 so rspamd's _rspamd user inside the container (uid != 99) can read it.
  # rspamd uses its own internal user, not the appdata 99:100 convention.
  chmod 644 "$TARGET"
  echo "rendered worker-controller.inc"
fi

echo "spamfilter bootstrap complete."
echo "Next:"
echo "  1. Edit $APP/accounts.yml (IMAP host, credentials)."
echo "  2. Install the four Docker templates from $BASE/unraid/."
