#!/usr/bin/env bash
# Install MapForge into a virtualenv on a Linux host (online).
#   scripts/install.sh [VENV_DIR]        default: ./.venv
# Needs python3 >= 3.10 with the venv module. No system GDAL required — the
# rasterio wheels bundle GDAL with the NITF/RPF/DTED/GTiff/COG/MBTiles drivers.
set -euo pipefail
here="$(cd "$(dirname "$0")/.." && pwd)"
venv="${1:-$here/.venv}"
py="${PYTHON:-python3}"

"$py" -c 'import sys; assert sys.version_info >= (3, 10), "Python 3.10+ required"'
"$py" -m venv "$venv"
"$venv/bin/python" -m pip install --upgrade pip >/dev/null
"$venv/bin/python" -m pip install "$here"
"$venv/bin/python" - <<'PY'
import rasterio
from rasterio.env import Env
with Env() as e:
    missing = [d for d in ("NITF", "RPFTOC", "ECRGTOC", "DTED", "GTiff", "COG", "MBTiles", "WMS", "WMTS") if d not in e.drivers()]
print("GDAL", rasterio.__gdal_version__, "- all required drivers present" if not missing else f"- MISSING drivers: {missing}")
PY
echo "Installed. Start with:  $venv/bin/mapforge   (or scripts/run.sh)"
