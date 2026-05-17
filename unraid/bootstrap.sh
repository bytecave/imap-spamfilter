#!/bin/bash
# imap-spamfilter Unraid bootstrap.
# Idempotent: safe to run any number of times. Set as User Scripts entry
# scheduled "At First Array Start Only" so the appdata layout and the
# shared `spamnet` Docker network are always in place before the
# spamfilter-* containers start.
set -euo pipefail

APP=/mnt/user/appdata/spamfilter
BASE=https://raw.githubusercontent.com/marcelverdult/imap-spamfilter/main
# Unraid container convention: appdata files belong to nobody:users.
APP_UID=99
APP_GID=100

# 1. Docker network shared by all four containers
docker network create spamnet 2>/dev/null || true

# 2. Directory layout under appdata.
# - Most dirs owned by nobody:users (Unraid appdata convention).
# - redis/ and rspamd/data/ are owned (recursively) by the official
#   images' internal uids (redis 999, rspamd _rspamd 11333) and kept
#   private (750) rather than world-writable (777), so a stray host
#   process cannot tamper with the Bayes corpus or rspamd's state.
# - state/ stays 755 (only the filter writes, runs as 99:100).
REDIS_UID=999      # uid of the redis user in redis:*-alpine
RSPAMD_UID=11333   # uid of _rspamd in rspamd/rspamd
mkdir -p "$APP"/{redis,state,rspamd/data,rspamd/local.d}
chown "$APP_UID:$APP_GID" "$APP" "$APP/state" "$APP/rspamd" "$APP/rspamd/local.d"
chmod 755 "$APP" "$APP/state" "$APP/rspamd" "$APP/rspamd/local.d"
chown -R "$REDIS_UID:$REDIS_UID" "$APP/redis"
chown -R "$RSPAMD_UID:$RSPAMD_UID" "$APP/rspamd/data"
chmod 750 "$APP/redis" "$APP/rspamd/data"

# 3. rspamd local.d configs (download only what's missing)
RSPAMD_FILES=(
  redis.conf.template
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

# 7. Redis auth. Generate a persistent Redis password, render the redis
#    server config (with requirepass) the redis container starts from,
#    and render rspamd's redis client config with the matching password.
REDIS_PW_FILE="$APP/state/redis.password"
if [ ! -f "$REDIS_PW_FILE" ]; then
  echo "generating Redis password"
  ( umask 077 && {
      openssl rand -base64 48 | tr -d '\n'
      echo
    } > "$REDIS_PW_FILE.tmp"
  )
  mv "$REDIS_PW_FILE.tmp" "$REDIS_PW_FILE"
fi
chown "$APP_UID:$APP_GID" "$REDIS_PW_FILE"
chmod 640 "$REDIS_PW_FILE"
REDIS_PW="$(cat "$REDIS_PW_FILE")"

# Redis server config: fetch the template if missing, substitute the
# password. Owned by the redis uid + 0640 so only that container reads it.
REDIS_CONF_TEMPLATE="$APP/redis.conf.template"
REDIS_CONF="$APP/redis.conf"
if [ ! -f "$REDIS_CONF_TEMPLATE" ]; then
  echo "fetching redis/redis.conf.template"
  curl -fsSL "$BASE/redis/redis.conf.template" -o "$REDIS_CONF_TEMPLATE"
fi
sed "s|\${REDIS_PASSWORD}|${REDIS_PW}|g" "$REDIS_CONF_TEMPLATE" > "$REDIS_CONF"
chown "$APP_UID:$APP_GID" "$REDIS_CONF_TEMPLATE"
chmod 644 "$REDIS_CONF_TEMPLATE"
chown "$REDIS_UID:$REDIS_UID" "$REDIS_CONF"
chmod 640 "$REDIS_CONF"

# rspamd redis client config: render the password into redis.conf from
# redis.conf.template (downloaded with the other rspamd local.d files).
REDIS_CLIENT_TEMPLATE="$APP/rspamd/local.d/redis.conf.template"
if [ -f "$REDIS_CLIENT_TEMPLATE" ]; then
  sed "s|\${REDIS_PASSWORD}|${REDIS_PW}|g" \
      "$REDIS_CLIENT_TEMPLATE" > "$APP/rspamd/local.d/redis.conf"
  chown "$APP_UID:$APP_GID" "$APP/rspamd/local.d/redis.conf"
  chmod 644 "$APP/rspamd/local.d/redis.conf"
  echo "rendered redis.conf (server + rspamd client)"
fi

echo "spamfilter bootstrap complete."
echo "Next:"
echo "  1. Edit $APP/accounts.yml (IMAP host, credentials)."
echo "  2. Install the four Docker templates from $BASE/unraid/."
