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
# ByteLord VPS: passwords live in /opt/bytelord/secrets/imap-spamfilter.env.
SPAMFILTER_SECRETS="${SPAMFILTER_SECRETS:-/opt/bytelord/secrets/imap-spamfilter.env}"
# accounts.yml path (ByteLord: repo checkout; Unraid: under APP).
SPAMFILTER_ACCOUNTS="${SPAMFILTER_ACCOUNTS:-$APP/accounts.yml}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
# Unraid container convention: appdata files belong to nobody:users.
APP_UID=99
APP_GID=100
# ByteLord VPS under /opt/bytelord/data: run as the deploy user, not uid 99.
if [[ "$APP" == /opt/bytelord/data/* ]]; then
  APP_UID="$(id -u)"
  APP_GID="$(id -g)"
fi

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
# Redis/rspamd image uids; skip if the deploy user cannot chown (containers
# still start when these dirs are group-writable on VPS).
chown -R "$REDIS_UID:$REDIS_UID" "$APP/redis" 2>/dev/null || true
chown -R "$RSPAMD_UID:$RSPAMD_UID" "$APP/rspamd/data" 2>/dev/null || true
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

read_env_secret() {
  # Read one KEY=value from SPAMFILTER_SECRETS (no expansion).
  local key="$1" file="$SPAMFILTER_SECRETS"
  [ -f "$file" ] || return 1
  local line
  line="$(grep -E "^[[:space:]]*(export[[:space:]]+)?${key}=" "$file" | tail -1)" || return 1
  line="${line#*=}"
  line="${line%%#*}"
  line="$(printf '%s' "$line" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' \
    -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//" -e 's/\r$//')"
  [ -n "$line" ] || return 1
  printf '%s' "$line"
}

write_password_file() {
  local dest="$1" value="$2"
  local tmp="${dest}.tmp"
  ( umask 077 && printf '%s\n' "$value" > "$tmp" )
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
if [ ! -f "$SPAMFILTER_ACCOUNTS" ]; then
  echo "seeding accounts.yml from accounts.yml.example -> $SPAMFILTER_ACCOUNTS"
  install_file "accounts.yml.example" "$SPAMFILTER_ACCOUNTS" 1
  echo
  echo "  >>> EDIT $SPAMFILTER_ACCOUNTS before starting the spamfilter container"
  echo
fi
# 640 so the filter container can read accounts.yml via the bind mount,
# while world has no access.
chown "$APP_UID:$APP_GID" "$SPAMFILTER_ACCOUNTS"
chmod 640 "$SPAMFILTER_ACCOUNTS"

# 5. Passwords come only from SPAMFILTER_SECRETS (ByteLord:
# /opt/bytelord/secrets/imap-spamfilter.env). Do not write copies into
# state/. Temp files for awk substitution are unlinked before exit.
if [ ! -f "$SPAMFILTER_SECRETS" ]; then
  echo "error: secrets file missing: $SPAMFILTER_SECRETS" >&2
  echo "  Required keys: RSPAMD_PASSWORD, REDIS_PASSWORD" >&2
  exit 1
fi
SEC_RSPAMD="$(read_env_secret RSPAMD_PASSWORD || true)"
SEC_REDIS="$(read_env_secret REDIS_PASSWORD || true)"
if [ -z "$SEC_RSPAMD" ] || [ -z "$SEC_REDIS" ]; then
  echo "error: $SPAMFILTER_SECRETS must set both RSPAMD_PASSWORD and REDIS_PASSWORD" >&2
  exit 1
fi
echo "using RSPAMD_PASSWORD and REDIS_PASSWORD from $SPAMFILTER_SECRETS"
TMP_RSPAMD="$(mktemp)"
TMP_REDIS="$(mktemp)"
cleanup_pw_tmp() { rm -f "$TMP_RSPAMD" "$TMP_REDIS"; }
trap cleanup_pw_tmp EXIT
write_password_file "$TMP_RSPAMD" "$SEC_RSPAMD"
write_password_file "$TMP_REDIS" "$SEC_REDIS"
unset SEC_RSPAMD SEC_REDIS
# Drop leftover Unraid-style copies if present.
rm -f "$APP/state/controller.password" "$APP/state/redis.password"

# 6. Render worker-controller.inc from the .template now (host side),
#    so the rspamd container can mount local.d/ as read-only and start
#    with the official entrypoint - no cp/envsubst gymnastics.
TEMPLATE="$APP/rspamd/local.d/worker-controller.inc.template"
TARGET="$APP/rspamd/local.d/worker-controller.inc"
if [ -f "$TEMPLATE" ]; then
  render_subst "$TEMPLATE" "$TARGET" '${RSPAMD_PASSWORD}' "$TMP_RSPAMD"
  chown "$RSPAMD_UID:$RSPAMD_UID" "$TARGET" 2>/dev/null || chown "$APP_UID:$APP_GID" "$TARGET"
  # 644: Docker user-ns maps container uids; 640 on host-owned files is unreadable.
  chmod 644 "$TARGET"
  echo "rendered worker-controller.inc"
fi

# 7. Redis server + rspamd Redis client configs.
REDIS_CONF_TEMPLATE="$APP/redis.conf.template"
REDIS_CONF_DIR="$APP/redis-config"
force_redis=0
if [ "$NEED_REFRESH" = "1" ]; then
  force_redis=1
fi
install_file "redis/redis.conf.template" "$REDIS_CONF_TEMPLATE" "$force_redis"
mkdir -p "$REDIS_CONF_DIR"
render_subst "$REDIS_CONF_TEMPLATE" "$REDIS_CONF_DIR/redis.conf" \
  '${REDIS_PASSWORD}' "$TMP_REDIS"
chown "$APP_UID:$APP_GID" "$REDIS_CONF_TEMPLATE"
chmod 644 "$REDIS_CONF_TEMPLATE"
chown -R "$REDIS_UID:$REDIS_UID" "$REDIS_CONF_DIR" 2>/dev/null \
  || chown -R "$APP_UID:$APP_GID" "$REDIS_CONF_DIR"
chmod 755 "$REDIS_CONF_DIR"
chmod 644 "$REDIS_CONF_DIR/redis.conf"
rm -f "$APP/redis.conf"

REDIS_CLIENT_TEMPLATE="$APP/rspamd/local.d/redis.conf.template"
if [ -f "$REDIS_CLIENT_TEMPLATE" ]; then
  render_subst "$REDIS_CLIENT_TEMPLATE" "$APP/rspamd/local.d/redis.conf" \
    '${REDIS_PASSWORD}' "$TMP_REDIS"
  chown "$RSPAMD_UID:$RSPAMD_UID" "$APP/rspamd/local.d/redis.conf" 2>/dev/null \
    || chown "$APP_UID:$APP_GID" "$APP/rspamd/local.d/redis.conf"
  chmod 644 "$APP/rspamd/local.d/redis.conf"
  echo "rendered redis.conf (server + rspamd client)"
fi

printf '%s\n' "$BOOTSTRAP_VERSION" > "$STAMP"
chown "$APP_UID:$APP_GID" "$STAMP"
chmod 644 "$STAMP"

echo "spamfilter bootstrap complete (config version $BOOTSTRAP_VERSION)."
echo "Next:"
echo "  1. Edit $SPAMFILTER_ACCOUNTS (IMAP host, credentials)."
echo "  2. docker compose -f /opt/bytelord/compose/imap-spamfilter/compose.yaml up -d --build"
