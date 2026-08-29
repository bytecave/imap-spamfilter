#!/usr/bin/env bash
# Fix Cursor Remote SSH terminal sandbox AppArmor profile on Ubuntu VPS.
# See: https://cursor.com/docs/agent/security/run-modes.md#apparmor-setup-remote-environments-and-cli-only
set -Eeuo pipefail

DEB_URL="https://downloads.cursor.com/lab/enterprise/cursor-sandbox-apparmor_0.6.0_all.deb"
DEB="/tmp/cursor-sandbox-apparmor.deb"
REMOTE_PROFILE="/etc/apparmor.d/cursor-sandbox-remote"
LOCAL_REMOTE="/etc/apparmor.d/local/cursor-sandbox-remote"
BACKUP_DIR="/root/apparmor-backups/cursor-$(date +%Y%m%d-%H%M%S)"

log() { printf '[%s] %s\n' "$(date +%F' '%T)" "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

[[ ${EUID:-$(id -u)} -eq 0 ]] || die "Run as root: sudo bash $0"

install -d -m 0755 "$BACKUP_DIR"
for f in "$REMOTE_PROFILE" "$LOCAL_REMOTE"; do
  [[ -e "$f" ]] && cp -a "$f" "$BACKUP_DIR/" && log "Backed up $f"
done

if [[ ! -f "$DEB" ]]; then
  log "Downloading cursor-sandbox-apparmor 0.6.0..."
  curl -fsSL "$DEB_URL" -o "$DEB"
fi

log "Installing cursor-sandbox-apparmor 0.6.0..."
dpkg -i "$DEB"

# 0.6.0 adds network/netlink rules but leaves userns commented on AppArmor 4.x.
# Wire local overrides (netlink dgram, dac_override) and enable userns.
log "Patching profile: enable userns + local includes..."
python3 - <<'PY'
from pathlib import Path

path = Path("/etc/apparmor.d/cursor-sandbox-remote")
text = path.read_text()

# Enable userns on AppArmor 4.x
text = text.replace("  ## Uncomment this on AppArmor 4.0\n  #userns,", "  userns,")

include_remote = "  #include if exists <local/cursor-sandbox-remote>\n"
include_agent = "  #include if exists <local/cursor-sandbox-agent-cli>\n"

if include_remote.strip() not in text:
    marker = "  /home/*/.cursor-server/bin/*/*/resources/helpers/cursorsandbox mr,\n"
    if marker in text:
        text = text.replace(marker, marker + "\n" + include_remote, 1)

if include_agent.strip() not in text:
    marker = "  /home/*/.local/share/cursor-agent/versions/*/cursorsandbox mr,\n"
    if marker in text:
        text = text.replace(marker, marker + "\n" + include_agent, 1)

path.write_text(text)
print("Profile patched.")
PY

log "Writing local override rules..."
install -d -m 0755 /etc/apparmor.d/local
tee "$LOCAL_REMOTE" >/dev/null <<'EOF'
  userns,
  network netlink raw,
  network netlink dgram,
  network unix stream,
  network unix dgram,
  capability dac_override,
EOF

log "Reloading AppArmor profiles..."
apparmor_parser -r "$REMOTE_PROFILE"

if command -v aa-status >/dev/null 2>&1; then
  aa-status 2>/dev/null | grep -i cursor || true
fi

log "Restarting cursor-server for clean profile attach..."
pkill -u "${SUDO_USER:-bytecave}" -f cursor-server 2>/dev/null || true

log "Done. Disconnect and reconnect Remote SSH in Cursor, then verify:"
echo "  sudo journalctl -k -b --since '2 minutes ago' --no-pager | grep -iE 'cursor_sandbox|apparmor=\"DENIED\"'"
