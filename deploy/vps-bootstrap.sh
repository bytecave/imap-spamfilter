#!/bin/bash
# ByteLord VPS bootstrap wrapper. Uses /opt/bytelord/data and secrets.
# accounts.yml lives in the git checkout (gitignored), not under data/.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export SPAMFILTER_APP="${SPAMFILTER_APP:-/opt/bytelord/data/imap-spamfilter}"
export SPAMFILTER_SECRETS="${SPAMFILTER_SECRETS:-/opt/bytelord/secrets/imap-spamfilter.env}"
export SPAMFILTER_ACCOUNTS="${SPAMFILTER_ACCOUNTS:-$ROOT/accounts.yml}"
export SPAMFILTER_UID="${SPAMFILTER_UID:-$(id -u)}"
export SPAMFILTER_GID="${SPAMFILTER_GID:-$(id -g)}"
exec bash "$ROOT/unraid/bootstrap.sh"
