#!/usr/bin/env bash
# Build a self-contained bundle for an air-gapped machine.
#   scripts/make-offline-bundle.sh [OUT_DIR]      default: ./dist/mapforge-offline
# Run this on an internet-connected machine with the SAME OS family, CPU architecture and
# Python minor version as the target (wheels are platform specific). Override the target with
#   PY_VERSION=3.12 PLATFORM=manylinux2014_x86_64 scripts/make-offline-bundle.sh
# Then copy the resulting .tar.gz across and on the offline machine run:
#   tar xzf mapforge-offline.tar.gz && cd mapforge-offline && ./install-offline.sh
set -euo pipefail
here="$(cd "$(dirname "$0")/.." && pwd)"
out="${1:-$here/dist/mapforge-offline}"
py="${PYTHON:-python3}"
rm -rf "$out"
mkdir -p "$out/wheels"

# 1. Build MapForge's own wheel, then download every dependency as a wheel.
"$py" -m pip wheel --no-deps -w "$out/wheels" "$here"
dl_args=(--dest "$out/wheels" --only-binary=:all:)
if [[ -n "${PLATFORM:-}" ]]; then dl_args+=(--platform "$PLATFORM"); fi
if [[ -n "${PY_VERSION:-}" ]]; then dl_args+=(--python-version "$PY_VERSION"); fi
"$py" -m pip download "${dl_args[@]}" "$here" pip setuptools wheel
rm -f "$out"/wheels/mapforge-*.tar.gz

# 2. Include the source tree and docs for reference / rebuilding.
mkdir -p "$out/source"
tar -C "$here" --exclude=.venv --exclude=data --exclude=dist --exclude=.git --exclude=.claude \
    --exclude='*.egg-info' --exclude=__pycache__ -cf - . | tar -C "$out/source" -xf -

# 3. Offline installer.
cat > "$out/install-offline.sh" <<'INNER'
#!/usr/bin/env bash
# Install MapForge with no network access.  ./install-offline.sh [VENV_DIR]
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
venv="${1:-$here/venv}"
"${PYTHON:-python3}" -m venv "$venv"
"$venv/bin/python" -m pip install --no-index --find-links "$here/wheels" --upgrade pip
"$venv/bin/python" -m pip install --no-index --find-links "$here/wheels" mapforge
echo "Installed. Start with: MAPFORGE_DATA=/srv/mapforge $venv/bin/mapforge --host 0.0.0.0"
INNER
chmod +x "$out/install-offline.sh"
tar -C "$(dirname "$out")" -czf "$out.tar.gz" "$(basename "$out")"
echo "Bundle: $out.tar.gz ($(du -h "$out.tar.gz" | cut -f1))"
