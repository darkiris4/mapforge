"""Fast XYZ tile download: MapForge fetches the tiles itself, then stitches them into one GeoTIFF.

GDAL's WMS driver opens a new TCP + TLS connection for every tile (verified with curl tracing;
no GDAL setting changes it). On a 5,500-tile job that meant ~3 tiles/s instead of ~25, and an
occasional connection reset failed a whole block. Here one pooled HTTP client reuses a handful of
connections, a failed tile is retried on its own, and progress counts real tiles.
"""
from __future__ import annotations

import math
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import httpx
import numpy as np
import rasterio
from rasterio.crs import CRS
from rasterio.enums import ColorInterp
from rasterio.io import MemoryFile
from rasterio.transform import Affine
from rasterio.windows import Window

from .. import speeds
from ..geo import BBox
from .base import Auth, Cancelled, Context

MERC = 20037508.342789244
WORKERS = 8  # parallel downloads over a shared connection pool
RETRY_DELAYS_S = (1, 3, 8, 20)  # per tile, for dropped connections / 5xx / 429
MAX_TILES = 250_000
KEEP_MOSAICS = 5  # stitched images kept per source for quick re-runs


def tile_range(bbox: BBox, z: int) -> tuple[int, int, int, int]:
    """(x0, y0, x1, y1) inclusive tile indices covering bbox at zoom z."""
    def tx(lon):
        return int((lon + 180.0) / 360.0 * 2**z)

    def ty(lat):
        lat = max(min(lat, 85.0511), -85.0511)
        return int((1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * 2**z)

    n = 2**z - 1
    x0, x1 = max(0, tx(bbox.west)), min(n, tx(bbox.east - 1e-12))
    y0, y1 = max(0, ty(bbox.north)), min(n, ty(bbox.south + 1e-12))
    return x0, y0, x1, y1


def tile_path(cache: Path, z: int, x: int, y: int) -> Path:
    return cache / str(z) / str(x) / f"{y}.tile"


def empty_marker(cache: Path, z: int, x: int, y: int) -> Path:
    return cache / str(z) / str(x) / f"{y}.none"


def is_cached(cache: Path, z: int, x: int, y: int) -> bool:
    return tile_path(cache, z, x, y).exists() or empty_marker(cache, z, x, y).exists()


def fetch_tiles(url: str, z: int, rng: tuple[int, int, int, int], cache: Path, ctx: Context,
                auth: Auth, name: str, sid: str) -> dict:
    """Download every tile in rng not already cached. Returns counts."""
    x0, y0, x1, y1 = rng
    tiles = [(x, y) for y in range(y0, y1 + 1) for x in range(x0, x1 + 1)]
    todo = [(x, y) for x, y in tiles if not is_cached(cache, z, x, y)]
    total, cached = len(tiles), len(tiles) - len(todo)
    ctx.progress(f"{name}: {cached:,} of {total:,} map tiles already here, fetching {len(todo):,}",
                 cached / total if total else 1.0)
    if not todo:
        return {"total": total, "fetched": 0, "cached": cached, "empty": 0}

    def get_one(c: httpx.Client, x: int, y: int) -> tuple[int, int]:
        """Returns (bytes written, 1 if empty)."""
        u = url.replace("{z}", str(z)).replace("{x}", str(x)).replace("{y}", str(y))
        last = None
        for attempt, delay in enumerate((0, *RETRY_DELAYS_S)):
            if delay:
                ctx.cancel_event.wait(delay)
            ctx.check()
            try:
                r = c.get(u)
            except httpx.TransportError as e:  # reset / timeout: retry just this tile
                last = e
                continue
            if r.status_code == 200 and r.content:
                p = tile_path(cache, z, x, y)
                p.parent.mkdir(parents=True, exist_ok=True)
                tmp = p.with_suffix(".part")
                tmp.write_bytes(r.content)
                tmp.replace(p)
                return len(r.content), 0
            if r.status_code in (204, 404) or (r.status_code == 200 and not r.content):
                m = empty_marker(cache, z, x, y)  # no imagery here: remember, don't re-ask
                m.parent.mkdir(parents=True, exist_ok=True)
                m.touch()
                return 0, 1
            if r.status_code in (401, 403):
                raise PermissionError(f"{name}: the server refused the request (HTTP {r.status_code}) — "
                                      "check credentials / certificate")
            last = f"HTTP {r.status_code}"
            if r.status_code == 429:  # asked to slow down
                ctx.cancel_event.wait(min(60.0, float(r.headers.get("retry-after", "5") or 5)))
        raise RuntimeError(f"{last}")

    fetched = empty = nbytes = failed = 0
    first_error = None
    t0 = time.monotonic()
    last_report = 0.0
    with ctx.http(auth, timeout=60) as c, ThreadPoolExecutor(WORKERS) as pool:
        futs = [pool.submit(get_one, c, x, y) for x, y in todo]
        for done, fut in enumerate(as_completed(futs), 1):
            try:
                b, e = fut.result()
                nbytes += b
                empty += e
                fetched += 1 - e
            except (PermissionError, Cancelled):
                for f in futs:
                    f.cancel()
                raise
            except Exception as e:  # noqa: BLE001 — count it, report once below
                failed += 1
                first_error = first_error or e
            now = time.monotonic()
            if now - last_report > 0.5 or done == len(todo):
                last_report = now
                rate = done / max(now - t0, 1e-6)
                ctx.progress(f"{name}: {cached + done:,} of {total:,} map tiles ({rate:.0f}/s)",
                             (cached + done) / total)
    elapsed = time.monotonic() - t0
    if fetched:
        speeds.record(ctx.settings, speeds.host_key("tiles", url), fetched + empty, elapsed)
        speeds.observe(ctx.settings, f"tilebytes:{sid}", nbytes / fetched)
    if failed:
        raise RuntimeError(
            f"{name}: {failed:,} of {total:,} map tiles could not be downloaded after "
            f"{len(RETRY_DELAYS_S) + 1} tries each (last error: {first_error}). Everything already "
            "downloaded is kept — run the job again to fetch only what's missing.")
    return {"total": total, "fetched": fetched, "cached": cached, "empty": empty}


def _decode(data: bytes) -> tuple[np.ndarray, np.ndarray | None]:
    """Tile bytes -> (3 x H x W uint8 RGB, H x W alpha or None)."""
    with MemoryFile(data) as mem, mem.open() as ds:
        a = ds.read()
        ci = list(ds.colorinterp)
        if ds.count == 1 and ci[0] == ColorInterp.palette:
            lut = np.zeros((256, 4), np.uint8)
            for k, v in ds.colormap(1).items():
                if 0 <= k < 256:
                    lut[k] = v
            rgba = np.moveaxis(lut[a[0]], -1, 0)
            return rgba[:3], rgba[3]
    if a.shape[0] == 1:
        return np.repeat(a, 3, axis=0), None
    if a.shape[0] == 2:  # gray + alpha
        return np.repeat(a[:1], 3, axis=0), a[1]
    return a[:3], (a[3] if a.shape[0] >= 4 else None)


def build_mosaic(cache: Path, z: int, rng: tuple[int, int, int, int], ctx: Context, name: str) -> Path:
    """Stitch cached tiles into one Web Mercator GeoTIFF (JPEG + mask); reused if already built."""
    x0, y0, x1, y1 = rng
    out_dir = cache / "mosaics"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{z}_{x0}_{y0}_{x1}_{y1}.tif"
    if out.exists():
        out.touch()  # keep recently used mosaics when pruning
        return out
    first = next((tile_path(cache, z, x, y) for y in range(y0, y1 + 1) for x in range(x0, x1 + 1)
                  if tile_path(cache, z, x, y).exists()), None)
    if first is None:
        raise ValueError(f"{name}: the service has no imagery for this area at this level of detail")
    with MemoryFile(first.read_bytes()) as mem, mem.open() as ds:
        px = ds.width  # 256 for most services, 512 for some
    size = 2 * MERC / 2**z
    transform = Affine(size / px, 0, -MERC + x0 * size, 0, -size / px, MERC - y0 * size)
    nx, ny = x1 - x0 + 1, y1 - y0 + 1
    prof = dict(driver="GTiff", width=nx * px, height=ny * px, count=3, dtype="uint8",
                crs=CRS.from_epsg(3857), transform=transform, tiled=True, blockxsize=256, blockysize=256,
                compress="jpeg", jpeg_quality=90, photometric="YCBCR", BIGTIFF="IF_SAFER")
    tmp = out.with_suffix(".part.tif")
    total, done = nx * ny, 0
    with rasterio.Env(GDAL_TIFF_INTERNAL_MASK=True), rasterio.open(tmp, "w", **prof) as dst:
        for y in range(y0, y1 + 1):
            ctx.check()
            for x in range(x0, x1 + 1):
                win = Window((x - x0) * px, (y - y0) * px, px, px)
                p = tile_path(cache, z, x, y)
                if p.exists():
                    rgb, alpha = _decode(p.read_bytes())
                    if rgb.shape[1:] != (px, px):  # odd tile: skip rather than misplace
                        dst.write_mask(np.zeros((px, px), np.uint8), window=win)
                        continue
                    dst.write(rgb, window=win)
                    mask = np.full((px, px), 255, np.uint8) if alpha is None else np.where(alpha > 0, 255, 0).astype(np.uint8)
                    dst.write_mask(mask, window=win)
                else:
                    dst.write_mask(np.zeros((px, px), np.uint8), window=win)
                done += 1
            ctx.progress(f"{name}: assembling {done:,} of {total:,} tiles", done / total)
    tmp.replace(out)
    old = sorted(out_dir.glob("*.tif"), key=lambda p: p.stat().st_mtime, reverse=True)[KEEP_MOSAICS:]
    for p in old:
        p.unlink(missing_ok=True)
    return out
