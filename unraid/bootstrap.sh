#!/bin/bash
# imap-spamfilter bootstrap.
# Idempotent: safe to run any number of times. On Unraid, set it as a
# User Scripts entry scheduled "At First Array Start Only" so the
# appdata layout and the shared `spamnet` Docker network are always in
# place before the spamfilter-* containers start. For a non-Unraid
# host, run it once with SPAMFILTER_APP pointing at your appdata path.
set -euo pipefail

# Appdata root. Override with SPAMFILTER_APP on non-Unraid hosts.
APP="${SPAMFILTER_APP:-/mnt/user/appdata/spamfilter}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
# Unraid container convention: appdata files belong to nobody:users.
APP_UID=99
APP_GID=100

# Curl fallback when this script is not next to a checkout (User Scripts
# copy). Never use floating /main. Override with SPAMFILTER_REF / SPAMFILTER_REPO.
SPAMFILTER_REPO="${SPAMFILTER_REPO:-marcelverdult/imap-spamfilter}"
SPAMFILTER_REF="${SPAMFILTER_REF:-ca82e7fab6f00b5fa45fe95b3692025aa9842b0d}"
BASE="https://raw.githubusercontent.com/${SPAMFILTER_REPO}/${SPAMFILTER_REF}"

BOOTSTRAP_VERSION="$(tr -d '[:space:]' < "$SCRIPT_DIR/bootstrap.version" 2>/dev/null || true)"
if [ -z "$BOOTSTRAP_VERSION" ]; then
  # User Scripts often paste only this file. Keep the fallback in lockstep
  # with unraid/bootstrap.version so a re-paste can still trigger refresh.
  BOOTSTRAP_VERSION="$(tr -d '[:space:]' < "$REPO_ROOT/unraid/bootstrap.version" 2>/dev/null || echo 7)"
fi
STAMP="$APP/.bootstrap.version"
NEED_REFRESH=0
if [ ! -f "$STAMP" ] || [ "$(tr -d '[:space:]' < "$STAMP" 2>/dev/null || true)" != "$BOOTSTRAP_VERSION" ]; then
  NEED_REFRESH=1
fi

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

install_file() {
  # Copy from the git checkout when present; otherwise curl a pinned ref.
  # Always write dest.tmp then mv so an interrupted download is not
  # treated as a complete file on the next run.
  local rel="$1" dest="$2" force="${3:-0}"
  if [ -f "$dest" ] && [ "$force" != "1" ]; then
    return 0
  fi
  local tmp="${dest}.tmp"
  mkdir -p "$(dirname "$dest")"
  if [ -f "$REPO_ROOT/$rel" ]; then
    echo "copying $rel from checkout"
    cp "$REPO_ROOT/$rel" "$tmp"
  else
    echo "fetching $rel (ref $SPAMFILTER_REF)"
    curl -fsSL "$BASE/$rel" -o "$tmp"
  fi
  mv "$tmp" "$dest"
}

render_subst() {
  # Substitute ${PLACEHOLDER} from a password file. awk -v pfile= puts
  # the path on argv, never the secret (unlike sed "s|...|$PW|").
  local template="$1" dest="$2" placeholder="$3" pwfile="$4"
  local tmp="${dest}.tmp"
  awk -v pfile="$pwfile" -v ph="$placeholder" '
    BEGIN {
      if ((getline pw < pfile) < 0) exit 1
      close(pfile)
      gsub(/\r/, "", pw)
      sub(/\n$/, "", pw)
    }
    { gsub(ph, pw); print }
  ' "$template" > "$tmp"
  mv "$tmp" "$dest"
}

# 3. rspamd local.d configs
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
force_rspamd=0
if [ "$NEED_REFRESH" = "1" ]; then
  force_rspamd=1
fi
for f in "${RSPAMD_FILES[@]}"; do
  dst="$APP/rspamd/local.d/$f"
  install_file "rspamd/local.d/$f" "$dst" "$force_rspamd"
  chown "$APP_UID:$APP_GID" "$dst"
  chmod 644 "$dst"
done

# 4. accounts.yml seed (only if not present; user must edit afterwards)
if [ ! -f "$APP/accounts.yml" ]; then
  echo "seeding accounts.yml from accounts.yml.example"
  install_file "accounts.yml.example" "$APP/accounts.yml" 1
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
  render_subst "$TEMPLATE" "$TARGET" '${RSPAMD_PASSWORD}' "$PW_FILE"
  # rspamd is uid 11333; 640 so the secret is not world-readable.
  chown "$RSPAMD_UID:$RSPAMD_UID" "$TARGET"
  chmod 640 "$TARGET"
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

# Redis server config: fetch the template if missing (or on version
# refresh), substitute the password into a dedicated DIRECTORY. The
# redis container bind-mounts that directory (not the single file) -
# Unraid handles directory mounts reliably; single-file bind mounts it
# does not.
REDIS_CONF_TEMPLATE="$APP/redis.conf.template"
REDIS_CONF_DIR="$APP/redis-config"
force_redis=0
if [ "$NEED_REFRESH" = "1" ]; then
  force_redis=1
fi
install_file "redis/redis.conf.template" "$REDIS_CONF_TEMPLATE" "$force_redis"
mkdir -p "$REDIS_CONF_DIR"
render_subst "$REDIS_CONF_TEMPLATE" "$REDIS_CONF_DIR/redis.conf" \
  '${REDIS_PASSWORD}' "$REDIS_PW_FILE"
chown "$APP_UID:$APP_GID" "$REDIS_CONF_TEMPLATE"
chmod 644 "$REDIS_CONF_TEMPLATE"
# Dir + file owned by the redis uid, private (only that container reads it).
chown -R "$REDIS_UID:$REDIS_UID" "$REDIS_CONF_DIR"
chmod 750 "$REDIS_CONF_DIR"
chmod 640 "$REDIS_CONF_DIR/redis.conf"
# Drop the pre-directory loose copy an earlier bootstrap may have left.
rm -f "$APP/redis.conf"

# rspamd redis client config: render the password into redis.conf from
# redis.conf.template (downloaded with the other rspamd local.d files).
REDIS_CLIENT_TEMPLATE="$APP/rspamd/local.d/redis.conf.template"
if [ -f "$REDIS_CLIENT_TEMPLATE" ]; then
  render_subst "$REDIS_CLIENT_TEMPLATE" "$APP/rspamd/local.d/redis.conf" \
    '${REDIS_PASSWORD}' "$REDIS_PW_FILE"
  chown "$RSPAMD_UID:$RSPAMD_UID" "$APP/rspamd/local.d/redis.conf"
  chmod 640 "$APP/rspamd/local.d/redis.conf"
  echo "rendered redis.conf (server + rspamd client)"
fi

printf '%s\n' "$BOOTSTRAP_VERSION" > "$STAMP"
chown "$APP_UID:$APP_GID" "$STAMP"
chmod 644 "$STAMP"

echo "spamfilter bootstrap complete (config version $BOOTSTRAP_VERSION)."
echo "Next:"
echo "  1. Edit $APP/accounts.yml (IMAP host, credentials)."
echo "  2. Install the four Docker templates, or: docker compose --env-file /opt/bytelord/secrets/imap-spamfilter.env up -d"
