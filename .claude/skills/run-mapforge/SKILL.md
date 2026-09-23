---
name: run-mapforge
description: Launch, screenshot and stop the MapForge web app safely for manual checks or UI verification. Use this whenever you need to run the MapForge server, look at the UI, check a change in the browser, hit the HTTP API against a live server, or take screenshots of the Build/Jobs/Endpoints/Library tabs — even if the user just says "start it up", "show me the UI" or "does the page work".
---

# Run MapForge

MapForge is a FastAPI app (`mapforge.app:main`, console script `mapforge`) that serves the
web UI and `/api/*`. These scripts wrap the three things that went wrong when this was done by
hand: port/data collisions between parallel sessions, re-indexing FAA charts on every fresh
data dir (~1–2 min), and killing servers with `pkill -f`, which also kills the calling shell.

## Start

```bash
.claude/skills/run-mapforge/scripts/start.sh [PORT] [DATA_DIR]
```

- Defaults: port 8766, data dir `$CLAUDE_JOB_DIR/tmp/mapforge-<port>` (or `/tmp/...`).
- Uses the venv at `~/Projects/mapforge/.venv` (override with `MAPFORGE_VENV`).
- Seeds the FAA page + index cache from `MAPFORGE_SEED_CACHE` if set, otherwise from any
  existing `*/cache/faa` it finds under the job tmp dir, so coverage/footprints load instantly.
- Waits until `/api/info` answers, then prints `PID=<n> URL=<url> DATA=<dir>` and writes the PID
  to `<DATA_DIR>/server.pid`. Logs go to `<DATA_DIR>/server.log`.
- Pass extra env through as usual, e.g. `MAPFORGE_TOKEN=secret start.sh 8770`.

Pick a port nobody else is using: parallel sessions/agents each run their own server.

## Screenshot

```bash
.claude/skills/run-mapforge/scripts/screenshot.sh URL OUT.png [WIDTHxHEIGHT]
```

Headless Firefox with a throwaway profile (a shared/default profile fails when another Firefox
is open). Tabs are addressable by hash: `#build`, `#jobs`, `#endpoints`, `#library`. Then view
the PNG with the Read tool. A screenshot does not run interactions (drawing a box, clicking
Build) — for those, drive the API with curl, or use Playwright if it is installed in the venv.

Useful sizes: `1440x900` desktop, `390x844` phone. Blank grey squares on the map are just tiles
still loading at capture time, not a bug.

## Stop

```bash
.claude/skills/run-mapforge/scripts/stop.sh DATA_DIR     # or: kill <PID>
```

Never `pkill -f mapforge`: the pattern matches the shell running the command and kills it too.

## Quick API smoke test

```bash
curl -s localhost:PORT/api/sources | python3 -c "import json,sys; print(len(json.load(sys.stdin)), 'sources')"
curl -s -XPOST localhost:PORT/api/estimate -H 'content-type: application/json' \
  -d '{"bbox":[-77.1,38.85,-77.0,38.92],"layers":[{"source":"faa-tac"}],"outputs":{"geotiff":true}}'
```

To validate a finished package, use the `verify-map-output` skill.
