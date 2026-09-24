#!/usr/bin/env bash
# Start a MapForge server on its own port + data dir; print PID/URL once it answers.
set -euo pipefail
PORT="${1:-8766}"
BASE="${CLAUDE_JOB_DIR:-/tmp}/tmp"
DATA="${2:-$BASE/mapforge-$PORT}"
VENV="${MAPFORGE_VENV:-$HOME/Projects/mapforge/.venv}"
REPO="$(cd "$(dirname "$0")/../../../.." && pwd)"

if curl -s -o /dev/null "http://127.0.0.1:$PORT/"; then
  echo "port $PORT already in use — pick another" >&2; exit 1
fi
mkdir -p "$DATA/cache/faa"

# Seed FAA chart index so coverage doesn't re-index (~1-2 min) on a fresh data dir.
seed="${MAPFORGE_SEED_CACHE:-}"
if [[ -z "$seed" ]]; then
  seed="$(find "$BASE" -maxdepth 4 -type d -path '*/cache/faa' 2>/dev/null | grep -v "^$DATA/" | head -1 || true)"
fi
if [[ -n "$seed" && -d "$seed" ]]; then
  cp -n "$seed"/index_*.json "$seed"/page_*.html "$DATA/cache/faa/" 2>/dev/null || true
fi

cd "$REPO"
MAPFORGE_DATA="$DATA" nohup "$VENV/bin/python" -m mapforge.app --port "$PORT" >"$DATA/server.log" 2>&1 &
PID=$!
echo "$PID" >"$DATA/server.pid"
for _ in $(seq 1 60); do
  if curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/api/info" | grep -qE '200|401'; then
    echo "PID=$PID URL=http://127.0.0.1:$PORT/ DATA=$DATA"; exit 0
  fi
  if ! kill -0 "$PID" 2>/dev/null; then echo "server exited:" >&2; tail -20 "$DATA/server.log" >&2; exit 1; fi
  sleep 0.5
done
echo "server did not answer in 30s; log:" >&2; tail -20 "$DATA/server.log" >&2; exit 1
