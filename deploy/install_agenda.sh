#!/usr/bin/env bash
# Installs the twice-daily agenda notifier as a macOS LaunchAgent.
# Run once:  bash deploy/install_agenda.sh      (uninstall: ... uninstall)
set -euo pipefail
PLIST="com.wcf.agenda.plist"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$PLIST"
DEST="$HOME/Library/LaunchAgents/$PLIST"

if [[ "${1:-}" == "uninstall" ]]; then
    launchctl unload "$DEST" 2>/dev/null || true
    rm -f "$DEST"
    echo "Removed $DEST"
    exit 0
fi

mkdir -p "$HOME/Library/LaunchAgents"
cp "$SRC" "$DEST"
launchctl unload "$DEST" 2>/dev/null || true
launchctl load "$DEST"
echo "Installed + loaded $DEST"
echo "It will run at 09:00 and 17:00 daily and notify you when action is needed."
echo "You should get a test notification now (RunAtLoad)."
