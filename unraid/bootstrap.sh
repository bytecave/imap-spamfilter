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
# Generic/Unraid default. The ByteLord wrapper explicitly keeps the VPS-only
# /opt/bytelord/secrets/imap-spamfilter.env path.
SPAMFILTER_SECRETS="${SPAMFILTER_SECRETS:-$APP/secrets/imap-spamfilter.env}"
# accounts.yml path (ByteLord: repo checkout; Unraid: under APP).
SPAMFILTER_ACCOUNTS="${SPAMFILTER_ACCOUNTS:-$APP/accounts.yml}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
# Unraid container convention: appdata files belong to nobody:users.
APP_UID="${SPAMFILTER_UID:-99}"
APP_GID="${SPAMFILTER_GID:-100}"
# ByteLord VPS under /opt/bytelord/data: run as the deploy user, not uid 99.
if [[ "$APP" == /opt/bytelord/data/* ]]; then
  APP_UID="${SPAMFILTER_UID:-$(id -u)}"
  APP_GID="${SPAMFILTER_GID:-$(id -g)}"
fi

# Curl fallback when this script is not next to a checkout (User Scripts
# copy). Never use floating /main. A no-checkout install must explicitly set
# SPAMFILTER_REF to the same immutable commit used to obtain this script.
SPAMFILTER_REPO="${SPAMFILTER_REPO:-marcelverdult/imap-spamfilter}"
SPAMFILTER_REF="${SPAMFILTER_REF:-}"
BASE="https://raw.githubusercontent.com/${SPAMFILTER_REPO}/${SPAMFILTER_REF}"

BOOTSTRAP_VERSION="$(tr -d '[:space:]' < "$SCRIPT_DIR/bootstrap.version" 2>/dev/null || true)"
if [ -z "$BOOTSTRAP_VERSION" ]; then
  # User Scripts often paste only this file. Keep the fallback in lockstep
  # with unraid/bootstrap.version so a re-paste can still trigger refresh.
  BOOTSTRAP_VERSION="$(tr -d '[:space:]' < "$REPO_ROOT/unraid/bootstrap.version" 2>/dev/null || echo 9)"
fi
STAMP="$APP/.bootstrap.version"
NEED_REFRESH=0
if [ ! -f "$STAMP" ] || [ "$(tr -d '[:space:]' < "$STAMP" 2>/dev/null || true)" != "$BOOTSTRAP_VERSION" ]; then
  NEED_REFRESH=1
fi

# 1. Docker network shared by all four containers
docker network create spamnet 2>/dev/null || true
DOCKER_SECURITY_OPTIONS="$(docker info --format '{{json .SecurityOptions}}' 2>/dev/null || true)"
if printf '%s' "$DOCKER_SECURITY_OPTIONS" | grep -Eq 'name=(userns|rootless)'; then
  echo "error: Docker userns-remap/rootless mode is enabled; secure bind-mount ownership is host-mapped." >&2
  echo "  This bootstrap intentionally refuses to guess remapped UIDs/GIDs." >&2
  echo "  Use a deployment-specific mapped owner/group contract before continuing." >&2
  exit 1
fi

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
if [[ "$SPAMFILTER_SECRETS" == "$APP/"* ]]; then
  mkdir -p "$(dirname "$SPAMFILTER_SECRETS")"
  chown "$APP_UID:$APP_GID" "$(dirname "$SPAMFILTER_SECRETS")"
  chmod 700 "$(dirname "$SPAMFILTER_SECRETS")"
fi
# Prefer image ownership when bootstrap is privileged. For an unprivileged
# run, preserve data already owned by the image uid; otherwise prepare only
# deploy-user-owned data for the explicitly mapped appdata group. Never try a
# second recursive chown after an expected permission failure.
prepare_data_dir() {
  local path="$1" image_uid="$2" owner
  if [ "$(id -u)" -eq 0 ]; then
    chown -R "$image_uid:$image_uid" "$path"
    chmod -R u+rwX,g+rX,o-rwx "$path"
    return
  fi
  owner="$(stat -c '%u' "$path")"
  if [ "$owner" = "$image_uid" ]; then
    echo "preserving image-owned data directory: $path (uid $image_uid)"
    return
  fi
  if [ "$owner" = "$APP_UID" ]; then
    chgrp -R "$APP_GID" "$path"
    chmod -R u+rwX,g+rwX,o-rwx "$path"
    return
  fi
  echo "error: $path is owned by uid $owner; unprivileged bootstrap cannot migrate it." >&2
  echo "  Run once as root, or migrate it to image uid $image_uid before retrying." >&2
  return 1
}
prepare_data_dir "$APP/redis" "$REDIS_UID"
prepare_data_dir "$APP/rspamd/data" "$RSPAMD_UID"

fetch_source() {
  local rel="$1" dest="$2"
  mkdir -p "$(dirname "$dest")"
  if [ -f "$REPO_ROOT/$rel" ]; then
    echo "copying $rel from checkout"
    cp "$REPO_ROOT/$rel" "$dest"
  else
    if [[ ! "$SPAMFILTER_REF" =~ ^[0-9a-fA-F]{40}$ ]]; then
      echo "error: no checkout contains $rel and SPAMFILTER_REF is not a 40-character commit SHA" >&2
      echo "  Clone the repository, or set SPAMFILTER_REF to the immutable commit used for bootstrap.sh." >&2
      return 1
    fi
    echo "fetching $rel (ref $SPAMFILTER_REF)"
    curl --fail --silent --show-error --location \
      --connect-timeout 10 --max-time 90 --retry 3 --retry-delay 2 \
      --retry-connrefused "$BASE/$rel" -o "$dest"
  fi
  [ -s "$dest" ] || {
    echo "error: fetched empty file: $rel" >&2
    return 1
  }
}

install_file() {
  # Copy through a same-directory temporary and rename atomically. During a
  # version refresh, every source has already been staged and validated.
  local rel="$1" dest="$2" force="${3:-0}"
  if [ -f "$dest" ] && [ "$force" != "1" ]; then
    return 0
  fi
  local tmp="${dest}.tmp"
  mkdir -p "$(dirname "$dest")"
  rm -f "$tmp"
  if [ -n "${BUNDLE_STAGE:-}" ] && [ -f "$BUNDLE_STAGE/$rel" ]; then
    cp "$BUNDLE_STAGE/$rel" "$tmp"
  else
    fetch_source "$rel" "$tmp"
  fi
  mv "$tmp" "$dest"
}

render_subst() {
  # Substitute ${PLACEHOLDER} from a password file. awk -v pfile= puts
  # the path on argv, never the secret (unlike sed "s|...|$PW|").
  local template="$1" dest="$2" placeholder="$3" pwfile="$4"
  local tmp="${dest}.tmp"
  awk -v pfile="$pwfile" -v ph="$placeholder" '
    function literal_gsub(str, find, repl, pos, pre, post) {
      while ((pos = index(str, find)) > 0) {
        pre = substr(str, 1, pos - 1)
        post = substr(str, pos + length(find))
        str = pre repl post
      }
      return str
    }
    BEGIN {
      if ((getline pw < pfile) < 0) exit 1
      close(pfile)
      gsub(/\r/, "", pw)
      sub(/\n$/, "", pw)
    }
    { print literal_gsub($0, ph, pw) }
  ' "$template" > "$tmp"
  mv "$tmp" "$dest"
}

read_env_secret() {
  # Match filter.py's parser: trim whitespace, accept optional export, split
  # on the first "=", remove matching outer quotes, and do no expansion or
  # inline-comment stripping. Last duplicate wins.
  local key="$1" file="${2:-$SPAMFILTER_SECRETS}"
  [ -f "$file" ] || return 1
  awk -v wanted="$key" '
    function trim(s) {
      sub(/^[[:space:]]+/, "", s)
      sub(/[[:space:]]+$/, "", s)
      return s
    }
    {
      sub(/\r$/, "")
      s = trim($0)
      if (s == "" || substr(s, 1, 1) == "#") next
      if (s ~ /^export[[:space:]]+/) {
        sub(/^export[[:space:]]+/, "", s)
        s = trim(s)
      }
      eq = index(s, "=")
      if (!eq) next
      name = trim(substr(s, 1, eq - 1))
      value = trim(substr(s, eq + 1))
      if (length(value) >= 2) {
        first = substr(value, 1, 1)
        last = substr(value, length(value), 1)
        if ((first == "\"" || first == "\047") && last == first)
          value = substr(value, 2, length(value) - 2)
      }
      # _load_secret() applies a final strip after parsing.
      value = trim(value)
      if (name == wanted) {
        found = value
        seen = 1
      }
    }
    END {
      if (!seen || found == "") exit 1
      printf "%s", found
    }
  ' "$file"
}

write_password_file() {
  local dest="$1" value="$2"
  local tmp="${dest}.tmp"
  ( umask 077 && printf '%s\n' "$value" > "$tmp" )
  mv "$tmp" "$dest"
}

escape_config_string() {
  # Escape a one-line password for Redis/UCL double-quoted strings without
  # putting the secret on argv.
  local source="$1" dest="$2"
  awk '
    BEGIN {
      if ((getline value < ARGV[1]) < 0) exit 1
      close(ARGV[1])
      for (i = 1; i <= length(value); i++) {
        c = substr(value, i, 1)
        if (c == "\\" || c == "\"") printf "\\"
        printf "%s", c
      }
      printf "\n"
    }
  ' "$source" > "$dest"
  chmod 600 "$dest"
}

verify_secret_file() {
  local path="$1" expected_owner="$2" expected_group="$3"
  chmod 640 "$path"
  chown "$expected_owner:$expected_group" "$path"
  local actual
  actual="$(stat -c '%u:%g %a' "$path")"
  if [ "$actual" != "$expected_owner:$expected_group 640" ]; then
    echo "error: insecure rendered config metadata for $path: $actual" >&2
    return 1
  fi
}

parser_self_test() {
  local test_env
  test_env="$(mktemp)"
  ( umask 077 && cat > "$test_env" <<'EOF'
export RSPAMD_PASSWORD="  hash#value  "
REDIS_PASSWORD='slash\quote"value'
EOF
  )
  local rspamd redis result=0
  rspamd="$(read_env_secret RSPAMD_PASSWORD "$test_env" || true)"
  redis="$(read_env_secret REDIS_PASSWORD "$test_env" || true)"
  [ "$rspamd" = 'hash#value' ] || result=1
  [ "$redis" = 'slash\quote"value' ] || result=1
  rm -f "$test_env"
  return "$result"
}

# 3. Stage and validate the complete tracked configuration bundle before a
# refresh. This prevents a failed/partial download from activating a mixed set.
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
BUNDLE_STAGE=""
cleanup_stage() {
  [ -z "$BUNDLE_STAGE" ] || rm -rf "$BUNDLE_STAGE"
}
trap cleanup_stage EXIT

force_rspamd=0
if [ "$NEED_REFRESH" = "1" ]; then
  force_rspamd=1
  BUNDLE_STAGE="$(mktemp -d "$APP/.bootstrap-stage.XXXXXX")"
  for f in "${RSPAMD_FILES[@]}"; do
    fetch_source "rspamd/local.d/$f" "$BUNDLE_STAGE/rspamd/local.d/$f"
  done
  fetch_source "redis/redis.conf.template" "$BUNDLE_STAGE/redis/redis.conf.template"
  grep -Fq '${RSPAMD_PASSWORD}' "$BUNDLE_STAGE/rspamd/local.d/worker-controller.inc.template"
  grep -Fq '${REDIS_PASSWORD}' "$BUNDLE_STAGE/rspamd/local.d/redis.conf.template"
  grep -Fq '${REDIS_PASSWORD}' "$BUNDLE_STAGE/redis/redis.conf.template"
  echo "staged and validated complete config bundle"
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

# 5. Passwords come only from SPAMFILTER_SECRETS. For a fresh appdata-local
# Unraid install, generate a protected file using an unambiguous hex alphabet.
# ByteLord's external /opt secret is never generated or modified here.
if [ ! -f "$SPAMFILTER_SECRETS" ]; then
  if [[ "$SPAMFILTER_SECRETS" == "$APP/"* ]]; then
    echo "generating protected secrets file: $SPAMFILTER_SECRETS"
    ( umask 077
      {
        printf 'RSPAMD_PASSWORD='
        od -An -N32 -tx1 /dev/urandom | tr -d ' \n'
        printf '\nREDIS_PASSWORD='
        od -An -N32 -tx1 /dev/urandom | tr -d ' \n'
        printf '\n'
      } > "$SPAMFILTER_SECRETS"
    )
    chown "$APP_UID:$APP_GID" "$SPAMFILTER_SECRETS"
    chmod 600 "$SPAMFILTER_SECRETS"
  else
    echo "error: secrets file missing: $SPAMFILTER_SECRETS" >&2
    echo "  Required keys: RSPAMD_PASSWORD, REDIS_PASSWORD" >&2
    exit 1
  fi
fi
if [[ "$SPAMFILTER_SECRETS" == "$APP/"* ]]; then
  if ! chown "$APP_UID:$APP_GID" "$SPAMFILTER_SECRETS"; then
    echo "error: cannot assign app-local secrets to uid:gid $APP_UID:$APP_GID" >&2
    echo "  Restore the file with the expected owner or run bootstrap once as root." >&2
    exit 1
  fi
  chmod 600 "$SPAMFILTER_SECRETS"
fi
secret_mode="$(stat -c '%a' "$SPAMFILTER_SECRETS")"
if [ "${secret_mode: -2}" != "00" ]; then
  echo "error: secrets file must not be group/world accessible: $SPAMFILTER_SECRETS (mode $secret_mode)" >&2
  exit 1
fi
secret_owner="$(stat -c '%u' "$SPAMFILTER_SECRETS")"
secret_perm=$((8#$secret_mode))
if [ "$secret_owner" != "$APP_UID" ] || (( (secret_perm & 0400) == 0 )); then
  echo "error: filter uid $APP_UID cannot read protected secrets file: $SPAMFILTER_SECRETS" >&2
  echo "  External secrets are not modified; set owner uid $APP_UID and an owner-readable mode such as 600." >&2
  exit 1
fi
parser_self_test || {
  echo "error: bootstrap dotenv parser self-test failed" >&2
  exit 1
}
SEC_RSPAMD="$(read_env_secret RSPAMD_PASSWORD || true)"
SEC_REDIS="$(read_env_secret REDIS_PASSWORD || true)"
if [ -z "$SEC_RSPAMD" ] || [ -z "$SEC_REDIS" ]; then
  echo "error: $SPAMFILTER_SECRETS must set both RSPAMD_PASSWORD and REDIS_PASSWORD" >&2
  exit 1
fi
echo "using RSPAMD_PASSWORD and REDIS_PASSWORD from $SPAMFILTER_SECRETS"
TMP_RSPAMD="$(mktemp)"
TMP_REDIS="$(mktemp)"
TMP_RSPAMD_ESCAPED="$(mktemp)"
TMP_REDIS_ESCAPED="$(mktemp)"
cleanup_all() {
  rm -f "$TMP_RSPAMD" "$TMP_REDIS" "$TMP_RSPAMD_ESCAPED" "$TMP_REDIS_ESCAPED"
  cleanup_stage
}
trap cleanup_all EXIT
write_password_file "$TMP_RSPAMD" "$SEC_RSPAMD"
write_password_file "$TMP_REDIS" "$SEC_REDIS"
escape_config_string "$TMP_RSPAMD" "$TMP_RSPAMD_ESCAPED"
escape_config_string "$TMP_REDIS" "$TMP_REDIS_ESCAPED"
unset SEC_RSPAMD SEC_REDIS
# Drop leftover Unraid-style copies if present.
rm -f "$APP/state/controller.password" "$APP/state/redis.password"

# 6. Render worker-controller.inc from the .template now (host side),
#    so the rspamd container can mount local.d/ as read-only and start
#    with the official entrypoint - no cp/envsubst gymnastics.
TEMPLATE="$APP/rspamd/local.d/worker-controller.inc.template"
TARGET="$APP/rspamd/local.d/worker-controller.inc"
if [ -f "$TEMPLATE" ]; then
  render_subst "$TEMPLATE" "$TARGET" '${RSPAMD_PASSWORD}' "$TMP_RSPAMD_ESCAPED"
  verify_secret_file "$TARGET" "$APP_UID" "$APP_GID"
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
  '${REDIS_PASSWORD}' "$TMP_REDIS_ESCAPED"
chown "$APP_UID:$APP_GID" "$REDIS_CONF_TEMPLATE"
chmod 644 "$REDIS_CONF_TEMPLATE"
chown "$APP_UID:$APP_GID" "$REDIS_CONF_DIR"
chmod 755 "$REDIS_CONF_DIR"
verify_secret_file "$REDIS_CONF_DIR/redis.conf" "$APP_UID" "$APP_GID"
rm -f "$APP/redis.conf"

REDIS_CLIENT_TEMPLATE="$APP/rspamd/local.d/redis.conf.template"
if [ -f "$REDIS_CLIENT_TEMPLATE" ]; then
  render_subst "$REDIS_CLIENT_TEMPLATE" "$APP/rspamd/local.d/redis.conf" \
    '${REDIS_PASSWORD}' "$TMP_REDIS_ESCAPED"
  verify_secret_file "$APP/rspamd/local.d/redis.conf" "$APP_UID" "$APP_GID"
  echo "rendered redis.conf (server + rspamd client)"
fi
for rendered in \
  "$TARGET" \
  "$REDIS_CONF_DIR/redis.conf" \
  "$APP/rspamd/local.d/redis.conf"; do
  [ -s "$rendered" ] || {
    echo "error: rendered config is missing or empty: $rendered" >&2
    exit 1
  }
  if grep -Fq '${' "$rendered"; then
    echo "error: unresolved placeholder in rendered config: $rendered" >&2
    exit 1
  fi
done

printf '%s\n' "$BOOTSTRAP_VERSION" > "$STAMP"
chown "$APP_UID:$APP_GID" "$STAMP"
chmod 644 "$STAMP"

echo "spamfilter bootstrap complete (config version $BOOTSTRAP_VERSION)."
echo "Next:"
echo "  1. Edit $SPAMFILTER_ACCOUNTS (IMAP host, credentials)."
echo "  2. Start the stack using the Unraid templates or your selected Compose file."
