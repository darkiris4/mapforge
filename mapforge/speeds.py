"""Learned network speeds, used to turn download sizes into honest time estimates.

Every real download / export / tile fetch feeds a sample into an exponentially weighted
moving average (EWMA) keyed by what was measured, e.g. ``bytes:aeronav.faa.gov`` (bytes/s
from one host), ``chunks:usgs-naip`` (ImageServer exports/s) or ``tiles:tiles.maps.eox.at``
(tiles/s). Values persist in ``data/config/speeds.json`` so estimates improve with use.
Until a key has a measurement, estimates use conservative defaults and say so
(``speed_basis: "default"``).
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

ALPHA = 0.3  # weight of the newest sample
FILE = "speeds.json"

# Conservative fallbacks (per second) until something has been measured.
DEFAULTS = {
    "bytes": 1e6,  # 1 MB/s file downloads (FAA measured ~0.7 MB/s, S3 DEM tiles as low as 0.15 MB/s)
    "chunks": 2.0 / 60,  # ~2 ImageServer exports per minute (observed on USGS NAIP, 4 in parallel)
    "tiles": 10.0,  # map tiles per second
    "tilebytes": 25_000.0,  # average bytes per 256 px tile
}

_lock = threading.Lock()
_cache: dict[str, dict] = {}  # config-file path -> {key: {"value", "n", "updated"}}


def _path(settings) -> Path:
    return Path(settings.config_dir) / FILE


def _load(settings) -> dict:
    p = str(_path(settings))
    if p not in _cache:
        try:
            _cache[p] = json.loads(Path(p).read_text())
        except (OSError, ValueError):
            _cache[p] = {}
    return _cache[p]


def host_key(kind: str, url: str) -> str:
    return f"{kind}:{urlsplit(url).netloc or url}"


def observe(settings, key: str, value: float) -> None:
    """Fold one measurement into the running average for ``key`` and persist it."""
    if not (value > 0):
        return
    with _lock:
        data = _load(settings)
        cur = data.get(key)
        if cur:
            cur["value"] = (1 - ALPHA) * cur["value"] + ALPHA * value
            cur["n"] += 1
        else:
            data[key] = cur = {"value": value, "n": 1}
        cur["updated"] = time.time()
        p = _path(settings)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=1))
            tmp.replace(p)
        except OSError:
            pass  # a read-only config dir must never break a download


def record(settings, key: str, amount: float, seconds: float) -> None:
    """Record a throughput sample: ``amount`` units in ``seconds`` (stored per second)."""
    if seconds > 0.05 and amount > 0:
        observe(settings, key, amount / seconds)


def get(settings, key: str, default: float | None = None) -> tuple[float, str]:
    """(value, basis) where basis is "measured" or "default"."""
    with _lock:
        cur = _load(settings).get(key)
    if cur and cur.get("n", 0) >= 1:
        return float(cur["value"]), "measured"
    kind = key.split(":", 1)[0]
    return float(default if default is not None else DEFAULTS.get(kind, 1.0)), "default"


def reset_cache() -> None:
    """Forget in-memory copies (tests use fresh data dirs)."""
    with _lock:
        _cache.clear()
