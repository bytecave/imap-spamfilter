#!/bin/bash
# imap-spamfilter Unraid bootstrap.
# Idempotent: safe to run any number of times. Set as User Scripts entry
# scheduled "At First Array Start Only" so the appdata layout and the
# shared `spamnet` Docker network are always in place before the
# spamfilter-* containers start.
set -e

APP=/mnt/user/appdata/spamfilter
BASE=https://raw.githubusercontent.com/marcelverdult/imap-spamfilter/main

# 1. Docker network shared by all four containers
docker network create spamnet 2>/dev/null || true

# 2. Directory layout under appdata
mkdir -p "$APP"/{redis,state,rspamd/data,rspamd/local.d}

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
done

# 4. accounts.yml seed (only if not present; user must edit afterwards)
if [ ! -f "$APP/accounts.yml" ]; then
  echo "seeding accounts.yml from accounts.yml.example"
  curl -fsSL "$BASE/accounts.yml.example" -o "$APP/accounts.yml"
  chmod 600 "$APP/accounts.yml"
  echo
  echo "  >>> EDIT $APP/accounts.yml before starting the spamfilter container"
  echo
fi

echo "spamfilter bootstrap complete."
echo "Next:"
echo "  1. Edit $APP/accounts.yml (IMAP host, credentials)."
echo "  2. Edit $APP/rspamd/local.d/fuzzy_check.conf (replace the encryption-key placeholder)."
echo "  3. Install the four Docker templates from $BASE/unraid/."
