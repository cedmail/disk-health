#!/usr/bin/env bash
# Disk Health installer (macOS). Idempotent: re-run it any time to update the LaunchAgent.
#
#   ./install.sh            install / update and start the background service
#   ./install.sh --open     same, then open the dashboard and the Full Disk Access pane
#   ./install.sh --dry-run  print the LaunchAgent plist that would be written; change nothing
#
# From a git checkout the service runs from the checkout. From a release zip (RELEASE file present)
# the code is first copied to ~/Library/Application Support/disk-health/app, so the zip can go.
# Installed with Homebrew (`disk-health setup`), it runs from $(brew --prefix)/opt/disk-health/libexec,
# a path that survives `brew upgrade`.
set -euo pipefail

LABEL="com.diskhealth.agent"
OPEN=0
DRY_RUN=0
for arg in "$@"; do
  case "$arg" in
    --open) OPEN=1 ;;
    --dry-run) DRY_RUN=1 ;;
    -h|--help) sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown option: $arg" >&2; exit 2 ;;
  esac
done

say()  { printf '%s\n' "$*"; }
step() { printf '\n==> %s\n' "$*"; }
die()  { printf 'Error: %s\n' "$*" >&2; exit 1; }

# Escape a string for inclusion in XML text content.
# (sed rather than ${s//...}: bash 5.2 treats & in replacements specially, bash 3.2 does not.)
xml_escape() {
  printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g' \
    -e 's/"/\&quot;/g' -e "s/'/\\&apos;/g"
}

# ------------------------------------------------------------------ paths
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
[ -d "$PROJECT_DIR/diskhealth" ] || die "diskhealth/ not found next to install.sh ($PROJECT_DIR)"

DATA_DIR="${DISK_HEALTH_DATA:-$HOME/Library/Application Support/disk-health}"
LOG_DIR="$DATA_DIR/logs"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
APP_HOME="$DATA_DIR/app"          # where a release zip's code is installed

if [ "$DRY_RUN" -eq 0 ] && [ "$(uname -s)" != "Darwin" ]; then
  die "this installer is for macOS (use --dry-run to preview the plist elsewhere)"
fi

VERSION="$(cat "$PROJECT_DIR/VERSION" 2>/dev/null || echo "?")"
RELEASE=0
[ -f "$PROJECT_DIR/RELEASE" ] && RELEASE=1

# ------------------------------------------------------------------ release zip: copy to a stable place
if [ "$DRY_RUN" -eq 0 ] && [ "$RELEASE" -eq 1 ] \
   && [ "$PROJECT_DIR" != "$(cd "$APP_HOME" 2>/dev/null && pwd -P || true)" ]; then
  step "Installing Disk Health $VERSION into $APP_HOME"
  # A downloaded zip is quarantined: macOS would refuse to start the prebuilt launcher.
  xattr -dr com.apple.quarantine "$PROJECT_DIR" 2>/dev/null || true
  mkdir -p "$APP_HOME"
  rsync -a --delete --exclude __pycache__ "$PROJECT_DIR/" "$APP_HOME/"
  exec "$APP_HOME/install.sh" "$@"
fi

# Homebrew install: Cellar/disk-health/<version>/libexec changes on every upgrade; opt/ always points
# to the current version, so the LaunchAgent uses that.
RUN_DIR="$PROJECT_DIR"
BREW_FORMULA=0
case "$PROJECT_DIR" in
  */Cellar/disk-health/*/libexec)
    BREW_FORMULA=1
    OPT="${PROJECT_DIR%%/Cellar/*}/opt/disk-health/libexec"
    [ -d "$OPT" ] && RUN_DIR="$OPT" ;;
esac

if [ "$RELEASE" -eq 0 ] && [ "$BREW_FORMULA" -eq 0 ]; then
  case "$PROJECT_DIR/" in
    "$HOME/Downloads/"*|"$HOME/Desktop/"*|"$HOME/Documents/"*)
      say "Warning: $PROJECT_DIR is in a folder macOS protects (Downloads/Desktop/Documents)."
      say "         The service can't read its own code there until it has Full Disk Access."
      say "         Prefer a folder like ~/projects/disk-health." ;;
  esac
fi

# ------------------------------------------------------------------ python
BREW_PREFIX=""
if command -v brew >/dev/null 2>&1; then
  BREW_PREFIX="$(brew --prefix 2>/dev/null || true)"
else
  for p in /opt/homebrew /usr/local; do
    if [ -x "$p/bin/brew" ]; then BREW_PREFIX="$p"; break; fi
  done
fi

PYTHON=""
if [ -n "${DISK_HEALTH_PYTHON:-}" ]; then
  [ -x "$DISK_HEALTH_PYTHON" ] || die "DISK_HEALTH_PYTHON=$DISK_HEALTH_PYTHON is not executable"
  PYTHON="$DISK_HEALTH_PYTHON"
elif [ -n "$BREW_PREFIX" ] && [ -x "$BREW_PREFIX/bin/python3" ]; then
  PYTHON="$BREW_PREFIX/bin/python3"
elif [ -x /usr/bin/python3 ]; then
  # /usr/bin/python3 is only a stub until the Command Line Tools are installed.
  if [ "$DRY_RUN" -eq 0 ] && ! xcode-select -p >/dev/null 2>&1; then
    die "no python3: install Homebrew python (brew install python) or the Command Line Tools (xcode-select --install)"
  fi
  PYTHON="/usr/bin/python3"
else
  die "no python3 found (install Xcode Command Line Tools: xcode-select --install, or brew install python)"
fi

if ! "$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
  die "$PYTHON is $("$PYTHON" -V 2>&1); Python 3.9 or newer is required (brew install python)"
fi
PY_VERSION="$("$PYTHON" -c 'import platform; print(platform.python_version())')"
PY_REAL="$("$PYTHON" -c 'import sys,os;print(os.path.realpath(sys.executable))')"

# Port: from an existing config.json if set there, else 7777.
PORT=7777
if [ -f "$DATA_DIR/config.json" ]; then
  PORT="$("$PYTHON" -c 'import json,sys
try: print(int(json.load(open(sys.argv[1])).get("port", 7777)))
except Exception: print(7777)' "$DATA_DIR/config.json")"
fi
URL="http://127.0.0.1:$PORT"

# ------------------------------------------------------------------ launcher app
# Full Disk Access is granted to DiskHealth.app (stable path, survives Python upgrades);
# Python runs as its child and inherits the permission.
APP_DIR="$HOME/Applications/DiskHealth.app"
LAUNCHER="$APP_DIR/Contents/MacOS/DiskHealth"

build_launcher() {
  local src="$PROJECT_DIR/launcher/launcher.c" stamp="$APP_DIR/Contents/Resources/source.sha256" sum
  sum="$(shasum -a 256 "$src" | cut -d' ' -f1)"
  # Rebuild only when the source changed: a rebuilt binary is a "new app" for macOS
  # and would lose its Full Disk Access grant.
  if [ -x "$LAUNCHER" ] && [ -f "$stamp" ] && [ "$(cat "$stamp")" = "$sum" ]; then
    say "Launcher up to date: $APP_DIR"
    return
  fi
  local pre="$PROJECT_DIR/launcher/DiskHealth"
  mkdir -p "$APP_DIR/Contents/MacOS" "$APP_DIR/Contents/Resources"
  if [ -x "$pre" ] && [ -f "$pre.sha256" ] && [ "$(tr -d ' \n' < "$pre.sha256")" = "$sum" ]; then
    cp "$pre" "$LAUNCHER"              # prebuilt universal binary from the release zip
    say "Using the prebuilt launcher"
  else
    command -v cc >/dev/null 2>&1 || die "no C compiler: run xcode-select --install, then re-run ./install.sh"
    cc -O2 -Wall -o "$LAUNCHER" "$src"
  fi
  cat > "$APP_DIR/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleIdentifier</key><string>com.diskhealth.launcher</string>
  <key>CFBundleName</key><string>DiskHealth</string>
  <key>CFBundleExecutable</key><string>DiskHealth</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>LSUIElement</key><true/>
  <key>LSBackgroundOnly</key><true/>
</dict>
</plist>
PLIST
  codesign --force --sign - --identifier com.diskhealth.launcher "$APP_DIR" >/dev/null 2>&1 \
    || say "(codesign failed; the launcher still works but macOS may ask again after updates)"
  xattr -dr com.apple.quarantine "$APP_DIR" 2>/dev/null || true
  printf '%s' "$sum" > "$stamp"
  LAUNCHER_REBUILT=1
  say "Built $APP_DIR"
}
LAUNCHER_REBUILT=0

# ------------------------------------------------------------------ plist
AGENT_PATH="/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
if [ -n "$BREW_PREFIX" ] && [ "$BREW_PREFIX" != "/usr/local" ]; then
  AGENT_PATH="$BREW_PREFIX/bin:$BREW_PREFIX/sbin:$AGENT_PATH"
elif [ "$BREW_PREFIX" = "/usr/local" ]; then
  AGENT_PATH="/usr/local/sbin:$AGENT_PATH"
fi

EXTRA_ENV=""
if [ -n "${DISK_HEALTH_DATA:-}" ]; then
  EXTRA_ENV="    <key>DISK_HEALTH_DATA</key>
    <string>$(xml_escape "$DATA_DIR")</string>
"
fi

render_plist() {
  cat <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$(xml_escape "$LAUNCHER")</string>
    <string>$(xml_escape "$PYTHON")</string>
    <string>-m</string>
    <string>diskhealth</string>
    <string>serve</string>
  </array>
  <key>WorkingDirectory</key>
  <string>$(xml_escape "$RUN_DIR")</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>ProcessType</key>
  <string>Background</string>
  <key>LowPriorityIO</key>
  <true/>
  <key>Nice</key>
  <integer>10</integer>
  <key>StandardOutPath</key>
  <string>$(xml_escape "$LOG_DIR/agent.log")</string>
  <key>StandardErrorPath</key>
  <string>$(xml_escape "$LOG_DIR/agent.log")</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>$(xml_escape "$AGENT_PATH")</string>
    <key>HOME</key>
    <string>$(xml_escape "$HOME")</string>
    <key>LANG</key>
    <string>en_US.UTF-8</string>
    <key>PYTHONUNBUFFERED</key>
    <string>1</string>
${EXTRA_ENV}  </dict>
</dict>
</plist>
EOF
}

if [ "$DRY_RUN" -eq 1 ]; then
  render_plist
  exit 0
fi

# ------------------------------------------------------------------ install
step "Disk Health $VERSION"
say "Code: $RUN_DIR"

step "Python"
say "Using $PYTHON (Python $PY_VERSION)"
say "Real binary: $PY_REAL (started through DiskHealth.app)"

step "Launcher (DiskHealth.app)"
build_launcher

step "Data folder"
mkdir -p "$LOG_DIR"
if [ -f "$DATA_DIR/config.json" ]; then
  say "Keeping existing $DATA_DIR/config.json"
else
  cp "$PROJECT_DIR/config.example.json" "$DATA_DIR/config.json"
  say "Created $DATA_DIR/config.json (edit it to tune thresholds, then restart the agent)"
fi

step "LaunchAgent"
mkdir -p "$(dirname "$PLIST")"
render_plist > "$PLIST.tmp"
if command -v plutil >/dev/null 2>&1; then
  plutil -lint "$PLIST.tmp" >/dev/null || { rm -f "$PLIST.tmp"; die "generated plist is invalid"; }
fi
mv "$PLIST.tmp" "$PLIST"
say "Wrote $PLIST"

UID_NUM="$(id -u)"
launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || true
# bootout is asynchronous; give launchd a moment before re-bootstrapping.
for _ in 1 2 3 4 5; do
  launchctl print "gui/$UID_NUM/$LABEL" >/dev/null 2>&1 || break
  sleep 1
done
# setup means "run it": clear a launchctl disable left by an older install, then start it now
# (a loaded job can otherwise sit at "runs = 0" until the next login).
launchctl enable "gui/$UID_NUM/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UID_NUM" "$PLIST"
launchctl kickstart "gui/$UID_NUM/$LABEL" 2>/dev/null || true
say "Loaded and started $LABEL"

step "Waiting for the service"
OK=0
printf 'Waiting for %s ' "$URL"
for _ in $(seq 1 120); do
  if curl -sf --max-time 2 "$URL/api/status" >/dev/null 2>&1; then OK=1; break; fi
  printf '.'; sleep 0.5
done
echo
if [ "$OK" -eq 1 ]; then
  say "Service is up: $(curl -sf "$URL/api/status" | "$PYTHON" -c 'import json,sys
d=json.load(sys.stdin); print("%s free (%s%%), status %s" % (d.get("free_h", "?"), d.get("free_pct", "?"), d.get("level", "?")))' 2>/dev/null || echo ok)"
else
  say "The service did not answer on $URL within 60s."
  INFO="$(launchctl print "gui/$UID_NUM/$LABEL" 2>/dev/null || true)"
  RUNS="$(printf '%s\n' "$INFO" | sed -n 's/^[[:space:]]*runs = //p' | head -1)"
  if launchctl print-disabled "gui/$UID_NUM" 2>/dev/null | grep -Eq "\"$LABEL\" => (disabled|true)"; then
    say "macOS has the service disabled. Turn it on with:"
    say "  launchctl enable gui/$UID_NUM/$LABEL && disk-health restart"
  elif [ "${RUNS:-0}" = "0" ]; then
    say "launchd loaded it but never started it. Usually it is switched off as a background item:"
    say "  System Settings > General > Login Items & Extensions > Allow in the Background > DiskHealth (on)"
    say "then: disk-health restart   (or ./restart.sh)"
  else
    say "It started but isn't answering. Check the log: tail -50 \"$LOG_DIR/agent.log\""
    say "Is port $PORT already in use? lsof -nP -iTCP:$PORT -sTCP:LISTEN"
  fi
fi

# ------------------------------------------------------------------ next steps
cat <<EOF

------------------------------------------------------------------------
Next steps

1) Grant Full Disk Access to DiskHealth.app (recommended)
   Without it, du cannot read ~/Library/Containers, Mail, Safari, Messages,
   the Trash and other protected folders, so sizes are under-reported.

   System Settings > Privacy & Security > Full Disk Access > "+"
   then press Cmd+Shift+G, paste the path below and click Open:

       $APP_DIR

   (Grant the app, not Python: Python runs inside it and inherits the access,
   and the grant survives Homebrew Python upgrades.)

   Open the pane directly:
       open "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"

   Then restart the service so it picks up the permission:
       launchctl kickstart -k gui/$UID_NUM/$LABEL

$( [ "$LAUNCHER_REBUILT" -eq 1 ] && printf '   NOTE: the launcher was (re)built just now. If DiskHealth was already in the\n   list, remove it with "-" and add it again.\n' )

2) Chrome extension (badge + notifications)
   chrome://extensions > enable "Developer mode" > "Load unpacked" > select:

       $RUN_DIR/extension

3) Open the dashboard: $URL
   The first scan starts automatically within a minute and can take several
   minutes (du walks your home folder). Later scans run once a day.

$( if [ "$BREW_FORMULA" -eq 1 ]; then
     printf '4) Installed with Homebrew. Commands: disk-health status | open | restart | logs | uninstall\n'
     printf '   Update: brew upgrade disk-health (the service restarts itself on the new version).\n'
   elif [ "$RELEASE" -eq 1 ]; then
     printf '4) Installed from a release zip: you can delete the zip and its folder.\n'
     printf '   Restart / uninstall:  "%s/restart.sh"   "%s/uninstall.sh"\n' "$PROJECT_DIR" "$PROJECT_DIR"
     printf '   Update: unzip the new release and run its ./install.sh (data and settings are kept).\n'
   else
     printf '4) Update: cd "%s" && git pull && ./install.sh\n' "$PROJECT_DIR"
   fi )
------------------------------------------------------------------------
EOF

if [ "$OPEN" -eq 1 ]; then
  open "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles" || true
  open "$URL" || true
fi
