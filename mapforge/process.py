"""Mosaic, clip, reproject and write layers.

Every layer is rendered onto an EPSG:4326 (WGS84 geographic) grid, which TerraLens and most
military C2 map engines load natively alongside CADRG/CIB/DTED.  Rendering is done block by
block so areas far larger than RAM work.
"""
from __future__ import annotations

import json
import math
import shutil
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import rasterio
import rasterio.shutil
from rasterio.crs import CRS
from rasterio.enums import ColorInterp, Resampling
from rasterio.transform import Affine
from rasterio.vrt import WarpedVRT
from rasterio.windows import Window

from .geo import BBox, Grid, grid_for, interior_depth
from .sources.base import Context, Item, Source

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


def _native_res_m(ds) -> float:
    a = abs(ds.transform.a)
    if ds.crs and ds.crs.is_geographic:
        lat = (ds.bounds.top + ds.bounds.bottom) / 2
        return a * 111_320 * max(math.cos(math.radians(lat)), 0.1)
    return a


def open_item(item: Item, kind: str, target_res_m: float, stack: ExitStack) -> Opened | None:
    ds = stack.enter_context(rasterio.open(item.path))
    if ds.crs is None:
        return None
    native = _native_res_m(ds)
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
        with WarpedVRT(o.ds, **vrt_kw) as vrt:
            data = vrt.read(o.bands)
            if o.mode == "elevation":
                m = (data[0] != ELEV_NODATA) & np.isfinite(data[0])
            else:
                ab = o.alpha_band if o.alpha_band is not None else vrt.count
                m = vrt.read(ab) > 0

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


def _summarise_inputs(items: list[Item]) -> list[str]:
    labels = sorted({i.label or Path(i.path).name for i in items})
    return labels if len(labels) <= 12 else labels[:10] + [f"… and {len(labels) - 10} more"]


def build_layer(source: Source, bbox: BBox, lopts: LayerOptions, oopts: OutputOptions, out_dir: Path,
                ctx: Context, layer_name: str) -> dict:
    ctx.progress(f"{source.name}: locating data", None)
    items = source.items(bbox, lopts.res_m or source.default_res_m, ctx)
    if not items:
        return {"source": source.id, "name": source.name, "status": "empty",
                "message": "No data from this source intersects the area."}
    res_m = plan_resolution(source, items, lopts)
    grid = grid_for(bbox, res_m)
    if grid.pixels > ctx.settings.max_pixels:
        raise ValueError(f"{source.name}: {grid.width}x{grid.height} px at {res_m:g} m exceeds the "
                         f"{ctx.settings.max_pixels:,} pixel limit — use a coarser resolution or smaller area")
    out_dir.mkdir(parents=True, exist_ok=True)
    # Bounded HTTP timeouts so a dead tile server fails (or is cancelled) promptly.
    env = {"GDAL_CACHEMAX": 512, "GDAL_HTTP_MAX_RETRY": 3, "GDAL_HTTP_RETRY_DELAY": 2,
           "GDAL_HTTP_TIMEOUT": 60, "GDAL_HTTP_CONNECTTIMEOUT": 15,
           "GDAL_HTTP_USERAGENT": ctx.settings.user_agent}
    for it in items:
        env.update(it.gdal_env)
    result = {"source": source.id, "name": source.name, "kind": source.kind, "license": source.license,
              "res_m": round(res_m, 3), "width": grid.width, "height": grid.height,
              "bbox": list(bbox.as_tuple()), "crs": "EPSG:4326",
              "inputs": _summarise_inputs(items), "files": []}
    with rasterio.Env(**env), ExitStack() as stack:
        opened = [o for o in (open_item(i, source.kind, res_m, stack) for i in items) if o]
        if not opened:
            raise RuntimeError(f"{source.name}: none of the {len(items)} input(s) could be opened")
        tif = out_dir / f"{layer_name}.tif"
        need_tif = oopts.geotiff or oopts.cog or (oopts.mbtiles and source.kind == "rgb")
        if need_tif:
            stats = write_geotiff(tif, opened, source.kind, grid, lopts, source.resampling, ctx,
                                  oopts.overviews, source.name)
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
            result["files"] += write_dted(out_dir, opened, bbox, int(oopts.dted_level), ctx, source.name)
    for aux in out_dir.glob("*.aux.xml"):
        aux.unlink()
    result["status"] = "ok"
    (out_dir / "layer.json").write_text(json.dumps(result, indent=2))
    return result
