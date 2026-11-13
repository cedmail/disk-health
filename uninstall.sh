#!/usr/bin/env bash
# Disk Health uninstaller (macOS): stops the service and removes the LaunchAgent.
# Your data (history, config, logs) is only deleted if you confirm.
set -euo pipefail

LABEL="com.diskhealth.agent"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DATA_DIR="${DISK_HEALTH_DATA:-$HOME/Library/Application Support/disk-health}"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"

[ "$(uname -s)" = "Darwin" ] || { echo "This uninstaller is for macOS." >&2; exit 1; }

echo "==> Stopping $LABEL"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || echo "(not running)"

if [ -f "$PLIST" ]; then
  rm -f "$PLIST"
  echo "Removed $PLIST"
else
  echo "(no LaunchAgent at $PLIST)"
fi

APP_HOME="$DATA_DIR/app"
APP_DIR="$HOME/Applications/DiskHealth.app"
if [ -d "$APP_DIR" ]; then
  rm -rf "$APP_DIR"
  echo "Removed $APP_DIR"
fi
CODE_REMOVED=0
if [ -f "$APP_HOME/RELEASE" ]; then   # installed from a release zip (may be this very folder)
  rm -rf "$APP_HOME"
  CODE_REMOVED=1
  echo "Removed the installed code ($APP_HOME)"
fi

if [ -d "$DATA_DIR" ]; then
  echo
  echo "Data folder: $DATA_DIR"
  echo "(disk history database, config.json, API token, logs)"
  read -r -p "Delete it? [y/N] " answer || answer=""
  case "$answer" in
    y|Y|yes|YES)
      rm -rf "$DATA_DIR"
      echo "Deleted $DATA_DIR" ;;
    *)
      echo "Kept $DATA_DIR" ;;
  esac
fi

cat <<EOF

Done. Remaining manual steps:
  - Chrome: chrome://extensions > remove "Disk Health".
  - System Settings > Privacy & Security > Full Disk Access: remove the
    DiskHealth entry (it no longer points to anything).
$( case "$PROJECT_DIR" in
     */Cellar/disk-health/*) echo "  - Remove the Homebrew package too: brew uninstall disk-health" ;;
     *) [ "$CODE_REMOVED" -eq 1 ] && [ ! -d "$PROJECT_DIR" ] \
          && echo "  - The installed code was removed." \
          || echo "  - This folder is untouched: $PROJECT_DIR" ;;
   esac )
EOF
