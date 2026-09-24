#!/usr/bin/env bash
# Stop a server started by start.sh, by PID (never pkill -f: it matches the calling shell).
set -euo pipefail
DATA="${1:?usage: stop.sh DATA_DIR}"
PID="$(cat "$DATA/server.pid" 2>/dev/null || true)"
if [[ -z "$PID" ]]; then echo "no server.pid in $DATA" >&2; exit 1; fi
if kill "$PID" 2>/dev/null; then
  for _ in $(seq 1 20); do kill -0 "$PID" 2>/dev/null || break; sleep 0.25; done
  kill -9 "$PID" 2>/dev/null || true
  echo "stopped $PID"
else
  echo "process $PID not running"
fi
rm -f "$DATA/server.pid"
