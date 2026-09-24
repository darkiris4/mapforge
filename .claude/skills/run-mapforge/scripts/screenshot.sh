#!/usr/bin/env bash
# Headless Firefox screenshot with a throwaway profile. Usage: screenshot.sh URL OUT.png [WxH]
set -euo pipefail
URL="${1:?url}"; OUT="${2:?out.png}"; SIZE="${3:-1440x900}"
PROFILE="$(mktemp -d "${CLAUDE_JOB_DIR:-/tmp}/tmp/ffprof.XXXXXX" 2>/dev/null || mktemp -d)"
trap 'rm -rf "$PROFILE"' EXIT
OUT="$(cd "$(dirname "$OUT")" && pwd)/$(basename "$OUT")"
timeout 90 firefox --headless --no-remote --profile "$PROFILE" --window-size="${SIZE/x/,}" \
  --screenshot "$OUT" "$URL" >/dev/null 2>&1 || true
if [[ -s "$OUT" ]]; then echo "$OUT"; else echo "screenshot failed" >&2; exit 1; fi
