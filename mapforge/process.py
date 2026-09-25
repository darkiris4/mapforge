"""Mosaic, clip, reproject and write layers.

Every layer is rendered onto an EPSG:4326 (WGS84 geographic) grid, which TerraLens and most
military C2 map engines load natively alongside CADRG/CIB/DTED.  Rendering is done block by
block so areas far larger than RAM work.
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import time
from contextlib import ExitStack
from dataclasses import dataclass, field, replace
from pathlib import Path

import numpy as np
import rasterio
import rasterio.errors
import rasterio.shutil
from rasterio.crs import CRS
from rasterio.enums import ColorInterp, MaskFlags, Resampling
from rasterio.transform import Affine
from rasterio.vrt import WarpedVRT
from rasterio.windows import Window

from .geo import BBox, Grid, grid_for, interior_depth
from .sources.base import Context, Item, Source, TooManyTiles

WGS84 = CRS.from_epsg(4326)
BLOCK = 1024
ELEV_NODATA = -32767.0


@dataclass
class LayerOptions:
    res_m: float | None = None  # None -> native resolution of the finest contributing item
    compression: str = "auto"  # auto | jpeg | deflate | lzw | none
    jpeg_quality: int = 90


@dataclass
class OutputOptions:
    geotiff: bool = True
    cog: bool = False
    mbtiles: bool = False
    dted_level: int | None = None  # 0/1/2 for elevation layers
    overviews: bool = True


# --------------------------------------------------------------------------------------
# Opening sources
# --------------------------------------------------------------------------------------
@dataclass
class Opened:
    item: Item
    ds: rasterio.io.DatasetReader
    mode: str  # palette | gray | rgb | elevation
    lut: np.ndarray | None = None
    alpha_band: int | None = None
    native_res_m: float = 0.0
    bands: list[int] = field(default_factory=list)


def _is_mercator(crs) -> bool:
    if not crs or not crs.is_projected:
        return False
    if crs.to_epsg() in (3857, 3395, 900913, 3785):
        return True
    wkt = crs.to_wkt()
    return "Mercator" in wkt and "Transverse" not in wkt


def _native_res_m(ds, lat: float | None = None) -> float:
    """Ground resolution in metres at `lat` (default: the dataset centre)."""
    a = abs(ds.transform.a)
    if ds.crs and ds.crs.is_geographic:
        if lat is None:
            lat = (ds.bounds.top + ds.bounds.bottom) / 2
        return a * 111_320 * max(math.cos(math.radians(lat)), 0.1)
    if _is_mercator(ds.crs):
        # Mercator metres are stretched by 1/cos(lat): a z15 tile pixel is 4.8 m "map" but
        # only ~3.7 m on the ground at 39N.  Without this, tile zooms come out a level too fine.
        if lat is None:
            from rasterio.warp import transform_bounds
            b = transform_bounds(ds.crs, "EPSG:4326", *ds.bounds)
            lat = (b[1] + b[3]) / 2
        return a * max(math.cos(math.radians(lat)), 0.01)
    return a


def open_item(item: Item, kind: str, target_res_m: float, stack: ExitStack) -> Opened | None:
    ds = stack.enter_context(rasterio.open(item.path))
    if ds.crs is None:
        return None
    native = _native_res_m(ds, item.footprint.center_lat)
    # Use the coarsest overview (or tile zoom level) that is still at least as fine as the
    # target — crucial for tile services, whose full-resolution level may be zoom 19+.
    ovs = ds.overviews(1) if ds.count else []
    level = None
    for i, f in enumerate(ovs):
        if native * f <= target_res_m * 1.01:
            level = i
    if level is not None:
        ds = stack.enter_context(rasterio.open(item.path, overview_level=level))
        native = native * ovs[level]
    ci = list(ds.colorinterp)
    alpha = next((i + 1 for i, c in enumerate(ci) if c == ColorInterp.alpha), None)
    o = Opened(item, ds, "rgb", alpha_band=alpha, native_res_m=native)
    if kind == "elevation":
        o.mode, o.bands = "elevation", [1]
    elif ds.count == 1 and ci[0] == ColorInterp.palette:
        cmap = ds.colormap(1)
        lut = np.zeros((256, 4), np.uint8)
        for k, v in cmap.items():
            if 0 <= k < 256:
                lut[k] = v
        o.mode, o.lut, o.bands = "palette", lut, [1]
    elif ds.count - (1 if alpha else 0) < 3:
        o.mode, o.bands = "gray", [1]
    else:
        order = {ColorInterp.red: 0, ColorInterp.green: 1, ColorInterp.blue: 2}
        rgb = sorted([i + 1 for i, c in enumerate(ci) if c in order], key=lambda b: order[ci[b - 1]])
        o.bands = rgb if len(rgb) == 3 else [1, 2, 3]
    return o


def _to_uint8(a: np.ndarray, dtype: str) -> np.ndarray:
    if dtype == "uint8":
        return a
    if dtype == "uint16":
        top = 4095.0 if a.max(initial=0) <= 4095 else 65535.0
        return np.clip(a.astype(np.float32) * (255.0 / top), 0, 255).astype(np.uint8)
    return np.clip(a, 0, 255).astype(np.uint8)


def _prefetch(ds, bbox: BBox, check=None) -> None:
    """Read the source window so GDAL's WMS driver fetches its tiles in parallel (the warper
    otherwise requests them in small, serial chunks).  Read in bounded chunks so a cancel
    request is noticed between them instead of after a whole slow block."""
    from rasterio.warp import transform_bounds
    from rasterio.windows import from_bounds

    try:
        b = transform_bounds(WGS84, ds.crs, *bbox.as_tuple(), densify_pts=5)
        win = from_bounds(*b, transform=ds.transform).round_offsets().round_lengths()
        win = win.intersection(Window(0, 0, ds.width, ds.height))
    except Exception:
        return  # purely an optimisation
    if not 0 < win.width * win.height <= 64_000_000:
        return
    step = 2048
    for r in range(int(win.row_off), int(win.row_off + win.height), step):
        for c in range(int(win.col_off), int(win.col_off + win.width), step):
            if check:
                check()
            sub = Window(c, r, min(step, win.col_off + win.width - c), min(step, win.row_off + win.height - r))
            try:
                ds.read(window=sub)
            except Exception:
                pass


def _clip_mask(polys, transform: Affine, width: int, height: int) -> np.ndarray:
    """True where a pixel centre lies inside every clip polygon (lon/lat)."""
    from rasterio.features import geometry_mask

    m = np.ones((height, width), bool)
    for poly in polys:
        geom = {"type": "Polygon", "coordinates": [[list(p) for p in poly]]}
        m &= geometry_mask([geom], out_shape=(height, width), transform=transform, invert=True)
    return m


def _clip_bbox(polys, fp: BBox) -> BBox:
    """Footprint shrunk to the clip polygons, so mosaic priority is measured from the neatline."""
    out = fp
    for poly in polys:
        xs, ys = [p[0] for p in poly], [p[1] for p in poly]
        out = out.intersection(BBox(max(min(xs), -180), max(min(ys), -90), min(max(xs), 180), min(max(ys), 90))) or out
    return out


# --------------------------------------------------------------------------------------
# Rendering one grid window
# --------------------------------------------------------------------------------------
# Transient network failures from tile/WMS servers (a dropped connection, a timeout, a 5xx)
# surface as a RasterioIOError on read. GDAL only retries HTTP error codes itself, not a reset
# connection (verified in tests/test_resilience.py), and it keeps every tile it already fetched in
# its on-disk cache, so retrying the read re-downloads only what is missing.
NET_RETRY_DELAYS_S = (2, 5, 15, 30)
SERVICE_BLOCK = 1024  # px: 4x4 tiles per read, so progress updates every few seconds
_NET_ERR = re.compile(r"Unable to download block|Recv failure|Connection reset|Connection refused|"
                      r"timed out|Timeout was reached|Couldn't connect|Empty reply|SSL|"
                      r"HTTP status code: 5\d\d|Operation too slow", re.I)


class ServerDroppedError(RuntimeError):
    """A map server kept failing after all retries; message is written for end users."""


def _is_network_error(e: BaseException) -> bool:
    while e is not None:
        if _NET_ERR.search(str(e)):
            return True
        e = e.__cause__ or e.__context__
    return False


def read_with_retry(fn, check=None, what: str = "map server", note=None, delays=None):
    """Run a rasterio read, retrying transient network errors with cancellable back-off."""
    delays = NET_RETRY_DELAYS_S if delays is None else delays
    last = None
    for attempt, delay in enumerate((0, *delays)):
        if delay:
            if note:
                note(f"{what}: connection dropped — retrying ({attempt}/{len(delays)})")
            end = time.monotonic() + delay
            while time.monotonic() < end:
                if check:
                    check()
                time.sleep(min(0.5, max(0.0, end - time.monotonic())))
        try:
            return fn()
        except rasterio.errors.RasterioIOError as e:
            if not _is_network_error(e):
                raise
            last = e
    raise ServerDroppedError(
        f"The {what} kept dropping the connection while downloading ({len(delays) + 1} tries). "
        "Everything already downloaded is kept — run the job again to continue from where it stopped."
    ) from last


def render(opened: list[Opened], kind: str, transform: Affine, width: int, height: int,
           target_res_m: float, resampling: str, check=None) -> tuple[np.ndarray, np.ndarray]:
    """Composite all opened items into one window. Returns (data, valid_mask)."""
    bands = 1 if kind == "elevation" else 3
    out = np.full((bands, height, width), ELEV_NODATA if kind == "elevation" else 0,
                  np.float32 if kind == "elevation" else np.uint8)
    valid = np.zeros((height, width), bool)
    best = np.full((height, width), -np.inf, np.float32)
    win_bbox = BBox(transform.c, transform.f + transform.e * height, transform.c + transform.a * width, transform.f)
    cols = transform.c + (np.arange(width) + 0.5) * transform.a
    rows = transform.f + (np.arange(height) + 0.5) * transform.e
    xs, ys = np.meshgrid(cols, rows)

    for o in opened:
        if not win_bbox.intersects(o.item.footprint):
            continue
        if check:  # cancel between items: one slow tile service must not pin the job
            check()
        ratio = target_res_m / max(o.native_res_m, 1e-9)
        # Charts: sample nearest at up to 4x and box-average -> crisp but not aliased.
        f = int(min(4, max(1, round(ratio)))) if o.mode == "palette" or resampling == "nearest" else 1
        if o.mode == "elevation" or resampling != "nearest":
            rs = Resampling.average if ratio > 1.5 else Resampling.bilinear
        else:
            rs = Resampling.nearest
        if o.ds.driver in ("WMS", "WMTS"):
            _prefetch(o.ds, win_bbox, check)
            if check:
                check()
        vt = Affine(transform.a / f, 0, transform.c, 0, transform.e / f, transform.f)
        vrt_kw = dict(crs=WGS84, transform=vt, width=width * f, height=height * f, resampling=rs)
        if o.mode == "elevation":
            vrt_kw.update(src_nodata=o.ds.nodata if o.ds.nodata is not None else ELEV_NODATA,
                          nodata=ELEV_NODATA, dtype="float32")
        elif o.alpha_band is None:
            vrt_kw["add_alpha"] = True
        def read_window():
            with WarpedVRT(o.ds, **vrt_kw) as vrt:
                data = vrt.read(o.bands)
                if o.mode == "elevation":
                    return data, (data[0] != ELEV_NODATA) & np.isfinite(data[0])
                ab = o.alpha_band if o.alpha_band is not None else vrt.count
                return data, vrt.read(ab) > 0

        data, m = read_with_retry(read_window, check, o.item.label or "map server")

        if o.mode == "elevation":
            px, pm = data.astype(np.float32), m
        else:
            if o.mode == "palette":
                rgba = o.lut[data[0]]
                px = np.moveaxis(rgba[..., :3], -1, 0)
                if rgba[..., 3].any():
                    m &= rgba[..., 3] > 0
            elif o.mode == "gray":
                g = _to_uint8(data[0], o.ds.dtypes[0])
                px = np.stack([g, g, g])
            else:
                px = _to_uint8(data, o.ds.dtypes[0])
            if f > 1:
                px = ((px.reshape(3, height, f, width, f).sum(axis=(2, 4), dtype=np.uint16) + (f * f) // 2) // (f * f)).astype(np.uint8)
                pm = m.reshape(height, f, width, f).mean(axis=(1, 3)) >= 0.5
            else:
                pm = m

        fp = o.item.footprint
        if o.item.clip:
            pm = pm & _clip_mask(o.item.clip, transform, width, height)
            fp = _clip_bbox(o.item.clip, fp)
        depth = interior_depth(xs, ys, fp).astype(np.float32)
        take = pm & (depth > best)
        if take.any():
            out[:, take] = px[:, take]
            best[take] = depth[take]
            valid |= take
    return out, valid


# --------------------------------------------------------------------------------------
# Writers
# --------------------------------------------------------------------------------------
def _gtiff_profile(kind: str, grid: Grid, opts: LayerOptions, chart_like: bool) -> dict:
    comp = opts.compression
    if comp == "auto":
        comp = "deflate" if (kind == "elevation" or chart_like) else "jpeg"
    p = dict(driver="GTiff", width=grid.width, height=grid.height, crs=WGS84, transform=grid.transform,
             tiled=True, blockxsize=512, blockysize=512, BIGTIFF="IF_SAFER")
    if kind == "elevation":
        p.update(count=1, dtype="float32", nodata=ELEV_NODATA, compress="deflate" if comp == "jpeg" else comp,
                 predictor=3)
    else:
        p.update(count=3, dtype="uint8", photometric="RGB")
        if comp == "jpeg":
            p.update(compress="jpeg", photometric="YCBCR", jpeg_quality=opts.jpeg_quality)
        elif comp != "none":
            p.update(compress=comp, predictor=2)
    if p.get("compress") == "none":
        p.pop("compress")
    return p


def _overview_factors(w: int, h: int) -> list[int]:
    f, out = 2, []
    while max(w, h) / f >= 256:
        out.append(f)
        f *= 2
    return out


def write_geotiff(path: Path, opened: list[Opened], kind: str, grid: Grid, opts: LayerOptions,
                  resampling: str, ctx: Context, overviews: bool, label: str) -> dict:
    prof = _gtiff_profile(kind, grid, opts, resampling == "nearest")
    target_res = grid.yres * 111_320
    nblocks = math.ceil(grid.width / BLOCK) * math.ceil(grid.height / BLOCK)
    filled = 0
    with rasterio.Env(GDAL_TIFF_INTERNAL_MASK=True), rasterio.open(path, "w", **prof) as dst:
        n = 0
        for row in range(0, grid.height, BLOCK):
            for col in range(0, grid.width, BLOCK):
                ctx.check()
                w, h = min(BLOCK, grid.width - col), min(BLOCK, grid.height - row)
                t = grid.transform @ Affine.translation(col, row)
                data, valid = render(opened, kind, t, w, h, target_res, resampling, ctx.check)
                win = Window(col, row, w, h)
                dst.write(data, window=win)
                if kind != "elevation":
                    dst.write_mask((valid * 255).astype(np.uint8), window=win)
                filled += int(valid.sum())
                n += 1
                ctx.progress(f"{label}: rendering block {n}/{nblocks}", n / nblocks)
        if overviews:
            factors = _overview_factors(grid.width, grid.height)
            if factors:
                ctx.progress(f"{label}: building overviews", None)
                dst.build_overviews(factors, Resampling.average)
                dst.update_tags(ns="rio_overview", resampling="average")
    return {"coverage_pct": round(100 * filled / max(grid.pixels, 1), 2)}


def write_cog(src: Path, dst: Path, kind: str) -> None:
    with rasterio.open(src) as ds:
        comp = (ds.compression.value if ds.compression else "NONE").upper()
    if comp == "JPEG":
        opts = {"COMPRESS": "JPEG", "QUALITY": "90"}
    elif comp == "NONE":
        opts = {}
    else:  # keep lossless for charts and elevation
        opts = {"COMPRESS": comp, "PREDICTOR": "YES"}
    rasterio.shutil.copy(src, dst, driver="COG", BIGTIFF="IF_SAFER", **opts)


def write_mbtiles(src: Path, dst: Path, name: str) -> None:
    rasterio.shutil.copy(src, dst, driver="MBTiles", TILE_FORMAT="JPEG", QUALITY="90", NAME=name)
    with rasterio.open(dst, "r+") as ds:
        factors = _overview_factors(ds.width, ds.height)
        if factors:
            ds.build_overviews(factors, Resampling.average)


def dted_lon_multiplier(lat_south: int) -> int:
    a = abs(lat_south + 0.5)
    return 1 if a < 50 else 2 if a < 70 else 3 if a < 75 else 4 if a < 80 else 6


def dted_cell_grid(lat: int, lon: int, level: int) -> tuple[Affine, int, int]:
    """Pixel-is-point DTED grid for the 1°x1° cell with SW corner (lat, lon)."""
    lat_sec = {0: 30, 1: 3, 2: 1}[level]
    ny = 3600 // lat_sec + 1
    nx = 3600 // (lat_sec * dted_lon_multiplier(lat)) + 1
    dx, dy = 1.0 / (nx - 1), 1.0 / (ny - 1)
    return Affine(dx, 0, lon - dx / 2, 0, -dy, lat + 1 + dy / 2), nx, ny


DTED_SPACING_M = {0: 900.0, 1: 90.0, 2: 30.0}


def _dted_bbox(bbox: BBox) -> BBox:
    """Whole-degree cells covering bbox, plus the half-post margin DTED edge posts need."""
    m = 0.005
    return BBox(max(-180.0, math.floor(bbox.west) - m), max(-90.0, math.floor(bbox.south) - m),
                min(180.0, math.ceil(bbox.east) + m), min(90.0, math.ceil(bbox.north) + m))


def _covers(footprints: list[BBox], target: BBox, n: int = 21) -> bool:
    """True if the footprints jointly cover target (checked on an n x n point lattice)."""
    xs = np.linspace(target.west, target.east, n)
    ys = np.linspace(target.south, target.north, n)
    for x in xs:
        for y in ys:
            if not any(f.west <= x <= f.east and f.south <= y <= f.north for f in footprints):
                return False
    return True


def write_dted(out_dir: Path, opened: list[Opened], bbox: BBox, level: int, ctx: Context, label: str) -> list[str]:
    """Write standard DTED cells (dted/<wNNN>/<nNN>.dtL) covering bbox, complete 1° cells."""
    files = []
    cells = [(la, lo) for lo in range(math.floor(bbox.west), math.ceil(bbox.east))
             for la in range(math.floor(bbox.south), math.ceil(bbox.north))]
    tmp = out_dir / "_dted_tmp.tif"
    for i, (la, lo) in enumerate(cells, 1):
        ctx.check()
        ctx.progress(f"{label}: DTED{level} cell {i}/{len(cells)}", i / len(cells))
        t, nx, ny = dted_cell_grid(la, lo, level)
        data, valid = render(opened, "elevation", t, nx, ny, t.a * 111_320, "bilinear", ctx.check)
        if not valid.any():
            continue
        arr = np.where(valid, np.round(data[0]), ELEV_NODATA).astype(np.int16)
        with rasterio.open(tmp, "w", driver="GTiff", width=nx, height=ny, count=1, dtype="int16", crs=WGS84,
                           transform=t, nodata=int(ELEV_NODATA)) as d:
            d.write(arr, 1)
            d.update_tags(AREA_OR_POINT="Point")
        folder = out_dir / "dted" / f"{'e' if lo >= 0 else 'w'}{abs(lo):03d}"
        folder.mkdir(parents=True, exist_ok=True)
        dst = folder / f"{'n' if la >= 0 else 's'}{abs(la):02d}.dt{level}"
        rasterio.shutil.copy(tmp, dst, driver="DTED")
        files.append(str(dst.relative_to(out_dir)))
    tmp.unlink(missing_ok=True)
    for aux in out_dir.glob("dted/**/*.aux.xml"):
        aux.unlink()
    return files


# --------------------------------------------------------------------------------------
# Layer orchestration
# --------------------------------------------------------------------------------------
def plan_resolution(source: Source, items: list[Item], opts: LayerOptions) -> float:
    if opts.res_m:
        return max(float(opts.res_m), 0.05)
    natives = [i.native_res_m for i in items if i.native_res_m]
    return min(natives) if natives else source.default_res_m


FETCH_SHARE = 0.4  # share of a layer's progress bar given to downloading its inputs


def _phase(ctx: Context, lo: float, hi: float) -> Context:
    """A Context whose 0..1 progress fractions land in [lo, hi] of the layer's bar."""
    def progress(msg: str, frac: float | None = None) -> None:
        ctx.progress(msg, None if frac is None else lo + (hi - lo) * min(max(frac, 0.0), 1.0))
    return replace(ctx, progress=progress)


def _summarise_inputs(items: list[Item]) -> list[str]:
    labels = sorted({i.label or Path(i.path).name for i in items})
    return labels if len(labels) <= 12 else labels[:10] + [f"… and {len(labels) - 10} more"]


def build_layer(source: Source, bbox: BBox, lopts: LayerOptions, oopts: OutputOptions, out_dir: Path,
                ctx: Context, layer_name: str, mode: str = "kongsberg",
                clip_polygon: list[tuple[float, float]] | None = None) -> dict:
    if mode != "kongsberg":
        return build_layer_unconverted(source, bbox, lopts, out_dir, ctx, mode)
    # A layer's progress runs 0→1 across phases: fetching inputs, rendering, DTED.
    want_tif = oopts.geotiff or oopts.cog or (oopts.mbtiles and source.kind == "rgb")
    want_dted = source.kind == "elevation" and oopts.dted_level is not None
    render_end = 0.9 if (want_tif and want_dted) else 1.0
    ctx.progress(f"{source.name}: locating data", 0.0)
    items = source.items(bbox, lopts.res_m or source.default_res_m, _phase(ctx, 0.0, FETCH_SHARE))
    if not items:
        return {"source": source.id, "name": source.name, "status": "empty",
                "message": "No data from this source intersects the area."}
    if clip_polygon:
        # Fetching stays bbox-shaped (every Source works in rectangles); the drawn polygon is
        # applied here as an extra clip on top of whatever clip each item already carries (e.g.
        # an FAA chart's own neatline) — render() already ANDs together every polygon in
        # Item.clip, so this is the entire polygon-clipping implementation.
        for it in items:
            it.clip = [*(it.clip or []), clip_polygon]
    res_m = plan_resolution(source, items, lopts)
    grid = grid_for(bbox, res_m)
    if grid.pixels > ctx.settings.max_pixels:
        raise TooManyTiles(f"{source.name}: {grid.width}x{grid.height} px at {res_m:g} m exceeds the "
                           f"{ctx.settings.max_pixels:,} pixel limit — splitting the area into smaller pieces",
                           grid.pixels / ctx.settings.max_pixels)
    out_dir.mkdir(parents=True, exist_ok=True)
    # Bounded HTTP timeouts so a dead tile server fails (or is cancelled) promptly.
    env = {"GDAL_CACHEMAX": 512, "GDAL_HTTP_MAX_RETRY": 3, "GDAL_HTTP_RETRY_DELAY": 2,
           "GDAL_HTTP_TIMEOUT": 60, "GDAL_HTTP_CONNECTTIMEOUT": 15,
           "GDAL_HTTP_USERAGENT": ctx.settings.user_agent}
    for it in items:
        env.update(it.gdal_env)
    result = {"source": source.id, "name": source.name, "kind": source.kind, "license": source.license,
              "mode": "kongsberg", "res_m": round(res_m, 3), "width": grid.width, "height": grid.height,
              "bbox": list(bbox.as_tuple()), "crs": "EPSG:4326",
              "inputs": _summarise_inputs(items), "files": []}
    with rasterio.Env(**env), ExitStack() as stack:
        opened = [o for o in (open_item(i, source.kind, res_m, stack) for i in items) if o]
        if not opened:
            raise RuntimeError(f"{source.name}: none of the {len(items)} input(s) could be opened")
        tif = out_dir / f"{layer_name}.tif"
        need_tif = oopts.geotiff or oopts.cog or (oopts.mbtiles and source.kind == "rgb")
        if need_tif:
            stats = write_geotiff(tif, opened, source.kind, grid, lopts, source.resampling,
                                  _phase(ctx, FETCH_SHARE, render_end), oopts.overviews, source.name)
            result.update(stats)
            if oopts.cog:
                ctx.progress(f"{source.name}: writing COG", None)
                write_cog(tif, out_dir / f"{layer_name}_cog.tif", source.kind)
                result["files"].append(f"{layer_name}_cog.tif")
            if oopts.mbtiles and source.kind == "rgb":
                ctx.progress(f"{source.name}: writing MBTiles", None)
                write_mbtiles(tif, out_dir / f"{layer_name}.mbtiles", source.name)
                result["files"].append(f"{layer_name}.mbtiles")
            if oopts.geotiff:
                result["files"].insert(0, tif.name)
            else:
                tif.unlink()
        if source.kind == "elevation" and oopts.dted_level is not None:
            level = int(oopts.dted_level)
            dted_start = render_end if want_tif else FETCH_SHARE
            dted_bbox = _dted_bbox(bbox)
            dted_opened = opened
            if not _covers([i.footprint for i in items], dted_bbox):
                # Service sources (ImageServer, WMS, tiles) only returned the job area; DTED
                # cells are whole degrees, so fetch the expanded area at DTED post spacing.
                ctx.progress(f"{source.name}: fetching whole-degree area for DTED{level}", None)
                spacing_m = DTED_SPACING_M[level]
                d_items = source.items(dted_bbox, max(spacing_m, getattr(source, "min_res_m", 0) or 0),
                                       _phase(ctx, dted_start, dted_start + (1.0 - dted_start) / 2))
                dted_start += (1.0 - dted_start) / 2
                with rasterio.Env(**{k: v for i in d_items for k, v in i.gdal_env.items()}):
                    dted_opened = [o for o in (open_item(i, source.kind, spacing_m, stack) for i in d_items) if o]
            result["files"] += write_dted(out_dir, dted_opened, bbox, level, _phase(ctx, dted_start, 1.0),
                                          source.name)
    for aux in out_dir.glob("*.aux.xml"):
        aux.unlink()
    result["status"] = "ok"
    (out_dir / "layer.json").write_text(json.dumps(result, indent=2))
    return result


# --------------------------------------------------------------------------------------
# Un-converted output modes: "clipped" (cut in each file's own projection) and "original"
# (the files exactly as published).  Nothing is mosaicked or reprojected.
# --------------------------------------------------------------------------------------
MODES = ("kongsberg", "clipped", "original")
# Files that travel with a raster and describe it (world files, projection, metadata, overviews).
SIDECAR_SUFFIXES = (".tfw", ".tfwx", ".tifw", ".wld", ".jgw", ".j2w", ".pgw", ".prj", ".aux.xml", ".ovr",
                    ".htm", ".html", ".xml", ".txt", ".rrd", ".msk", ".tab")
_HASH_STEM = re.compile(r"[0-9a-f]{16,}")
_TOC_ENTRY = re.compile(r"^(NITF|ECRG)_TOC_ENTRY:(.*):([^:]*(?:A\.TOC|TOC\.xml))$", re.I)


def _safe_stem(s: str) -> str:
    s = re.sub(r"[^\w .,()+-]+", "_", s).strip(" ._")
    return s[:80] or "file"


def _item_stem(item: Item) -> str:
    if item.service:  # built from a map service: name it after the layer, not the internal file
        return _safe_stem(item.label or Path(item.path).stem)
    m = _TOC_ENTRY.match(item.path)
    if m:
        return _safe_stem(m.group(2).replace(":", "-"))
    p = Path(item.path)
    if item.path.startswith("/vsicurl/"):
        return _safe_stem(Path(item.path.split("?")[0]).stem)
    if p.suffix and not item.path.startswith(("/vsi", "WMTS:")) and not _HASH_STEM.fullmatch(p.stem) \
            and p.suffix.lower() != ".xml":
        return _safe_stem(p.stem)
    return _safe_stem(item.label or p.stem)


def _unique(out_dir: Path, name: str) -> Path:
    p = out_dir / name
    n = 2
    while p.exists():
        stem, dot, ext = name.partition(".")
        p = out_dir / f"{stem} ({n}){dot}{ext}"
        n += 1
    return p


def crs_label(crs) -> str:
    """Short, human-readable projection name, e.g. 'EPSG:4326 (WGS 84)' or 'Lambert Conformal Conic'."""
    if crs is None:
        return "no georeferencing"
    epsg = crs.to_epsg()
    wkt = crs.to_wkt()
    m = re.match(r'\s*(?:PROJCS|PROJCRS|GEOGCS|GEOGCRS|GEODCRS)\["([^"]+)"', wkt)
    name = m.group(1) if m else crs.to_string()
    return f"EPSG:{epsg} ({name})" if epsg else name


def _format_label(ds) -> str:
    return {"GTiff": "GeoTIFF", "DTED": "DTED", "NITF": "NITF", "JP2OpenJPEG": "JPEG 2000"}.get(ds.driver, ds.driver)


def _tree_size(p: Path) -> int:
    if p.is_dir():
        return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
    return p.stat().st_size if p.exists() else 0


def describe_file(path: Path, rel: str, source_label: str = "") -> dict:
    """Plain facts about one output file, for layer.json / manifest / README."""
    info = {"file": rel, "size_mb": round(_tree_size(path) / 1e6, 2), "source": source_label}
    if path.is_dir():
        info["format"] = "folder (whole product as published)"
        return info
    try:
        with rasterio.open(path) as ds:
            palette = ds.count == 1 and ds.colorinterp[0] == ColorInterp.palette
            info.update(format=_format_label(ds), crs=crs_label(ds.crs), width=ds.width, height=ds.height,
                        bands=ds.count, dtype=ds.dtypes[0], palette=palette)
    except Exception:
        info["format"] = "sidecar / metadata"
    return info


def _is_service(item: Item) -> bool:
    """True for tile/WMS/WMTS services, which publish no files to copy."""
    if item.service or item.path.startswith("WMTS:"):
        return True
    if item.path.lower().endswith(".xml") and "_TOC_ENTRY:" not in item.path:
        try:
            with open(item.path, "rb") as f:
                return b"<GDAL_WMS" in f.read(4096)
        except OSError:
            return False
    return False


def _link_or_copy(src: Path, dst: Path) -> None:
    """Hard-link when src and dst share a filesystem (instant, no extra disk), else copy."""
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def _source_window(ds, bbox: BBox) -> Window | None:
    """Pixel window of ds covering bbox (lon/lat), in ds's own CRS; None if no overlap."""
    from rasterio.errors import WindowError
    from rasterio.warp import transform_bounds
    from rasterio.windows import from_bounds

    if ds.crs is None:
        return None
    b = transform_bounds(WGS84, ds.crs, *bbox.as_tuple(), densify_pts=21)
    w = from_bounds(*b, transform=ds.transform)
    c0, r0 = math.floor(w.col_off), math.floor(w.row_off)
    c1, r1 = math.ceil(w.col_off + w.width), math.ceil(w.row_off + w.height)
    try:
        win = Window(c0, r0, c1 - c0, r1 - r0).intersection(Window(0, 0, ds.width, ds.height))
    except WindowError:
        return None
    return win if win.width >= 1 and win.height >= 1 else None




def write_clip(ds, bbox: BBox, dst_path: Path, ctx: Context, is_service: bool = False) -> bool:
    """Cut ds to bbox without reprojecting: same CRS, dtype, bands, palette and nodata.

    Lossless (DEFLATE) unless the source itself was JPEG-compressed imagery; tile services
    are written as JPEG with their transparency kept as an internal mask.  Returns False when
    ds does not overlap bbox.
    """
    win = _source_window(ds, bbox)
    if win is None:
        return False
    ci = list(ds.colorinterp)
    palette = ds.count == 1 and ci[0] == ColorInterp.palette
    alpha = next((i + 1 for i, c in enumerate(ci) if c == ColorInterp.alpha), None)
    src_comp = (ds.compression.value if ds.compression else "NONE").upper()
    dtype = ds.dtypes[0]
    colour = [b for b in range(1, ds.count + 1) if b != alpha]
    jpeg = dtype == "uint8" and len(colour) in (1, 3) and not palette and (src_comp == "JPEG" or is_service)
    # Lossless outputs keep an alpha band as-is; JPEG can't carry one, so it becomes a mask.
    bands = colour if jpeg else list(range(1, ds.count + 1))
    width, height = int(win.width), int(win.height)
    prof = dict(driver="GTiff", width=width, height=height, count=len(bands), dtype=dtype, crs=ds.crs,
                transform=ds.window_transform(win), nodata=ds.nodata, tiled=True, blockxsize=512,
                blockysize=512, BIGTIFF="IF_SAFER")
    if jpeg:
        prof.update(compress="jpeg", jpeg_quality=90, photometric="YCBCR" if len(bands) == 3 else "MINISBLACK")
    else:
        prof["compress"] = "deflate"
        if not palette:
            prof["predictor"] = 3 if np.dtype(dtype).kind == "f" else 2
    mask_from = None
    if ds.nodata is None:
        if jpeg and alpha:
            mask_from = "alpha"
        elif alpha is None and any(MaskFlags.per_dataset in f for f in ds.mask_flag_enums):
            mask_from = "mask"
    # Map services fetch tiles during each read, so read them in small blocks (4x4 tiles) and
    # report after each one: the bar moves every few seconds. Files are read in big strips.
    rows = max(1, min(height, (64 << 20) // max(1, width * len(bands) * np.dtype(dtype).itemsize)))
    cols = width
    if is_service:
        rows = cols = SERVICE_BLOCK
    blocks = [(r, c) for r in range(0, height, rows) for c in range(0, width, cols)]
    what = "map server" if is_service else "source file"
    with rasterio.Env(GDAL_TIFF_INTERNAL_MASK=True), rasterio.open(dst_path, "w", **prof) as dst:
        if palette:
            dst.write_colormap(1, ds.colormap(1))
        elif not jpeg:
            dst.colorinterp = [ci[b - 1] for b in bands]
        for i, b in enumerate(bands, 1):
            if ds.descriptions[b - 1]:
                dst.set_band_description(i, ds.descriptions[b - 1])
        for n, (r, c) in enumerate(blocks, 1):
            ctx.check()
            h, w = min(rows, height - r), min(cols, width - c)
            src_win = Window(win.col_off + c, win.row_off + r, w, h)
            dst_win = Window(c, r, w, h)
            dst.write(read_with_retry(lambda: ds.read(bands, window=src_win), ctx.check, what,
                                      lambda m: ctx.progress(m, None)), window=dst_win)
            if mask_from:
                m = read_with_retry(lambda: ds.read(alpha, window=src_win) if mask_from == "alpha"
                                    else ds.read_masks(1, window=src_win), ctx.check, what)
                dst.write_mask(np.where(m > 0, 255, 0).astype(np.uint8), window=dst_win)
            ctx.progress(f"Cutting {dst_path.name} — part {n} of {len(blocks)}", n / len(blocks))
        factors = _overview_factors(width, height)
        if factors:  # internal overviews only speed up display; the pixels themselves are untouched
            dst.build_overviews(factors, Resampling.nearest if palette else Resampling.average)
        dst.update_tags(MAPFORGE_MODE="clipped")
    return True


def _original_files(item: Item, source: Source, ctx: Context) -> tuple[list[Path], Path | None]:
    """The published file(s) behind an item: (files, tree_root). tree_root is set for RPF/ECRG
    products, whose frames live in a directory tree that must be copied whole."""
    m = _TOC_ENTRY.match(item.path)
    if m:
        return [], Path(m.group(3)).parent
    if item.path.startswith("/vsicurl/"):
        url = item.path[len("/vsicurl/"):]
        name = Path(url.split("?")[0]).name
        # "Original" means the whole published file, even where only a corner was read remotely.
        folder = ctx.settings.cache_dir / ("dem" if hasattr(source, "tile_url") else "originals") / source.id
        return [ctx.download(url, folder / name, label=name)], None
    p = Path(item.path)
    if not p.is_file():
        return [], None
    sidecars = [f for f in p.parent.iterdir()
                if f != p and f.is_file() and f.name.startswith(p.stem + ".")
                and any(f.name.lower().endswith(s) for s in SIDECAR_SUFFIXES)]
    return [p, *sorted(sidecars)], None


def build_layer_unconverted(source: Source, bbox: BBox, lopts: LayerOptions, out_dir: Path, ctx: Context,
                            mode: str) -> dict:
    """Layer for the 'clipped' and 'original' modes: no mosaic, no reprojection."""
    if mode not in ("clipped", "original"):
        raise ValueError(f"unknown mode {mode!r}")
    ctx.progress(f"{source.name}: locating data", 0.0)
    items = source.items(bbox, lopts.res_m or source.default_res_m, _phase(ctx, 0.0, FETCH_SHARE))
    if not items:
        return {"source": source.id, "name": source.name, "status": "empty", "mode": mode,
                "message": "No data from this source intersects the area."}
    res_m = plan_resolution(source, items, lopts)
    out_dir.mkdir(parents=True, exist_ok=True)
    env = {"GDAL_CACHEMAX": 512, "GDAL_HTTP_MAX_RETRY": 3, "GDAL_HTTP_RETRY_DELAY": 2,
           "GDAL_HTTP_TIMEOUT": 60, "GDAL_HTTP_CONNECTTIMEOUT": 15,
           "GDAL_HTTP_USERAGENT": ctx.settings.user_agent}
    for it in items:
        env.update(it.gdal_env)
    files: list[str] = []
    file_info: list[dict] = []
    copied_trees: set[Path] = set()
    skipped = 0
    work = _phase(ctx, FETCH_SHARE, 1.0)
    with rasterio.Env(**env):
        for n, item in enumerate(items):
            ctx.check()
            step = _phase(work, n / len(items), (n + 1) / len(items))
            label = item.label or _item_stem(item)
            with ExitStack() as stack:
                is_service = _is_service(item)
                if mode == "original" and not is_service:
                    step.progress(f"{source.name}: copying {label}", 0.0)
                    srcs, tree = _original_files(item, source, step)
                    if tree is not None:
                        if tree not in copied_trees:
                            copied_trees.add(tree)
                            dst = _unique(out_dir, _safe_stem(tree.name))
                            shutil.copytree(tree, dst, copy_function=lambda s, d: _link_or_copy(Path(s), Path(d)))
                            files.append(dst.name + "/")
                            file_info.append(describe_file(dst, dst.name + "/", label))
                        continue
                    if not srcs:
                        skipped += 1
                        continue
                    for i, src in enumerate(srcs):
                        dst = out_dir / src.name
                        if dst.exists():  # the same file reached through two items
                            continue
                        _link_or_copy(src, dst)
                        files.append(dst.name)
                        if i == 0:
                            file_info.append(describe_file(dst, dst.name, label))
                    continue
                # clipped mode, and services in original mode: cut in the data's own projection
                ds = stack.enter_context(rasterio.open(item.path))
                if is_service:
                    o = open_item(item, source.kind, res_m, stack)  # the zoom level matching res_m
                    ds = o.ds if o else ds
                dst = _unique(out_dir, f"{_item_stem(item)}_clip.tif")
                step.progress(f"{source.name}: cutting {label}", 0.0)
                if not write_clip(ds, bbox, dst, step, is_service=is_service):
                    skipped += 1
                    continue
                files.append(dst.name)
                file_info.append(describe_file(dst, dst.name, label))
    for aux in out_dir.glob("*.aux.xml"):
        if aux.name not in files:
            aux.unlink()
    if not files:
        return {"source": source.id, "name": source.name, "status": "empty", "mode": mode,
                "message": "None of the source files overlap the area."}
    total_mb = round(sum(f["size_mb"] for f in file_info), 2)
    what = "cut to your area in its own projection" if mode == "clipped" else "exactly as published"
    result = {"source": source.id, "name": source.name, "kind": source.kind, "license": source.license,
              "mode": mode, "res_m": round(res_m, 3), "bbox": list(bbox.as_tuple()),
              "crs": "native (see file_info)", "inputs": _summarise_inputs(items), "files": files,
              "file_info": file_info, "size_mb": total_mb,
              "summary": f"{len(file_info)} file(s) {what}, {total_mb:g} MB", "status": "ok"}
    if skipped:
        result["summary"] += f" ({skipped} input(s) had nothing inside the area)"
    (out_dir / "layer.json").write_text(json.dumps(result, indent=2))
    ctx.progress(f"{source.name}: done", 1.0)
    return result
