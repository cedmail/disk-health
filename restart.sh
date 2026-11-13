#!/usr/bin/env bash
# Restart the Disk Health background service and wait until it answers.
#
#   ./restart.sh          restart (loads the agent first if it isn't loaded)
#   ./restart.sh --logs   restart, then follow the service log (Ctrl+C to stop)
#   ./restart.sh --stop   stop the service until the next login or ./restart.sh
set -euo pipefail

LABEL="com.diskhealth.agent"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DATA_DIR="${DISK_HEALTH_DATA:-$HOME/Library/Application Support/disk-health}"
LOG="$DATA_DIR/logs/agent.log"
DOMAIN="gui/$(id -u)"

PORT=7777
if [ -f "$DATA_DIR/config.json" ]; then
  PORT="$(/usr/bin/python3 -c 'import json,sys
try: print(int(json.load(open(sys.argv[1])).get("port", 7777)))
except Exception: print(7777)' "$DATA_DIR/config.json" 2>/dev/null || echo 7777)"
fi
URL="http://127.0.0.1:$PORT"

case "${1:-}" in
  --stop)
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null && echo "Stopped $LABEL." || echo "$LABEL was not running."
    exit 0 ;;
  ""|--logs) ;;
  -h|--help) sed -n '2,7p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
  *) echo "Unknown option: $1" >&2; exit 2 ;;
esac

[ -f "$PLIST" ] || { echo "LaunchAgent not installed ($PLIST). Run ./install.sh first." >&2; exit 1; }

if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
  echo "Restarting $LABEL ..."
  launchctl kickstart -k "$DOMAIN/$LABEL"
  echo "(a running scan or clean is stopped; the service can take up to a minute to come back)"
else
  echo "Loading $LABEL ..."
  launchctl bootstrap "$DOMAIN" "$PLIST"
fi

printf 'Waiting for %s ' "$URL"
for _ in $(seq 1 120); do
  if STATUS="$(curl -sf --max-time 2 "$URL/api/status")"; then
    echo "ok"
    printf '%s' "$STATUS" | /usr/bin/python3 -c 'import json,sys
d = json.load(sys.stdin); p = d.get("progress") or {}
busy = ", scanning %d%%" % round(100 * (p.get("pct") or 0)) if p.get("active") else ""
print("%s free (%s%%), status: %s%s" % (d.get("free_h", "?"), d.get("free_pct", "?"), d.get("level", "?"), busy))' 2>/dev/null || true
    echo "Dashboard: $URL"
    [ "${1:-}" = "--logs" ] && { echo "--- $LOG (Ctrl+C to stop) ---"; tail -n 20 -f "$LOG"; }
    exit 0
  fi
  printf '.'; sleep 0.5
done
echo " no answer after 60s."
echo "Last log lines ($LOG):"
tail -n 20 "$LOG" 2>/dev/null || echo "(no log yet)"
echo "Port in use? lsof -nP -iTCP:$PORT -sTCP:LISTEN"
exit 1
