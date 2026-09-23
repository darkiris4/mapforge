#!/usr/bin/env bash
# Run MapForge from the project virtualenv.
#   scripts/run.sh [--host 0.0.0.0] [--port 8765] [--data DIR] [--library DIR ...]
# Environment: MAPFORGE_DATA, MAPFORGE_LIBRARY, MAPFORGE_TOKEN, MAPFORGE_HOST, MAPFORGE_PORT,
#              MAPFORGE_MAX_PIXELS, MAPFORGE_WORKERS (see README.md).
set -euo pipefail
here="$(cd "$(dirname "$0")/.." && pwd)"
venv="${MAPFORGE_VENV:-$here/.venv}"
if [[ ! -x "$venv/bin/mapforge" ]]; then
  echo "MapForge is not installed in $venv — run scripts/install.sh first" >&2
  exit 1
fi
export MAPFORGE_DATA="${MAPFORGE_DATA:-$here/data}"
exec "$venv/bin/mapforge" "$@"
