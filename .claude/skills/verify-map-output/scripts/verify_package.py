#!/usr/bin/env python3
"""Validate a MapForge package and render a contact sheet.

usage: verify_package.py PACKAGE_DIR|LAYER_DIR [--sheet OUT.png] [--json]

Checks every file listed in each layer's layer.json:
  GeoTIFF   EPSG:4326, tiled, overviews, sensible compression, bounds match the package bbox,
            band/dtype layout, valid-pixel coverage
  COG       LAYOUT=COG
  MBTiles   opens, has zoom levels
  DTED      grid size for its level + latitude zone, post spacing, whole-degree extent,
            voids, plausible elevations
Exit status 1 if any check FAILs (WARNs do not fail).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import warnings

import numpy as np
import rasterio
import rasterio.errors
from rasterio.enums import Resampling

warnings.filterwarnings("ignore", category=rasterio.errors.NotGeoreferencedWarning)

RESULTS: list[tuple[str, str, str]] = []  # (status, file, message)


def rec(status: str, f: str, msg: str) -> None:
    RESULTS.append((status, f, msg))


def dted_expected(level: int, lat_south: int) -> tuple[int, int]:
    a = abs(lat_south + 0.5)
    mult = 1 if a < 50 else 2 if a < 70 else 3 if a < 75 else 4 if a < 80 else 6
    lat_sec = {0: 30, 1: 3, 2: 1}[level]
    return 3600 // (lat_sec * mult) + 1, 3600 // lat_sec + 1


def check_tif(p: Path, rel: str, layer: dict, bbox) -> None:
    with rasterio.open(p) as ds:
        prof = ds.profile
        if ds.crs is None or ds.crs.to_epsg() != 4326:
            rec("FAIL", rel, f"CRS is {ds.crs}, expected EPSG:4326")
        if not prof.get("tiled"):
            rec("FAIL", rel, "not tiled")
        ovr = ds.overviews(1)
        if max(ds.width, ds.height) > 1024 and not ovr:
            rec("WARN", rel, f"{ds.width}x{ds.height} but no overviews")
        comp = (ds.compression.value if ds.compression else "NONE").upper()
        kind = layer.get("kind", "rgb")
        if kind == "elevation":
            if ds.count != 1 or ds.dtypes[0] != "float32":
                rec("FAIL", rel, f"elevation should be 1-band float32, got {ds.count}x{ds.dtypes[0]}")
            if ds.nodata is None:
                rec("WARN", rel, "elevation has no nodata value")
            if comp == "JPEG":
                rec("FAIL", rel, "elevation JPEG-compressed (lossy)")
        else:
            if ds.count < 3 or ds.dtypes[0] != "uint8":
                rec("FAIL", rel, f"imagery should be >=3-band uint8, got {ds.count}x{ds.dtypes[0]}")
        if bbox:
            b = ds.bounds
            tol_x, tol_y = abs(ds.transform.a) * 1.01, abs(ds.transform.e) * 1.01
            off = [abs(b.left - bbox[0]) / tol_x, abs(b.bottom - bbox[1]) / tol_y,
                   abs(b.right - bbox[2]) / tol_x, abs(b.top - bbox[3]) / tol_y]
            if max(off) > 1:
                rec("FAIL", rel, f"bounds {tuple(round(v, 6) for v in b)} differ from package bbox by >1 px")
        # coverage from mask / nodata on a decimated read
        scale = max(1, max(ds.width, ds.height) // 1024)
        shape = (max(1, ds.height // scale), max(1, ds.width // scale))
        if kind == "elevation":
            a = ds.read(1, out_shape=shape, resampling=Resampling.nearest)
            valid = (a != ds.nodata) & np.isfinite(a) if ds.nodata is not None else np.isfinite(a)
            if valid.any():
                lo, hi = float(a[valid].min()), float(a[valid].max())
                if lo < -500 or hi > 9000:
                    rec("WARN", rel, f"elevation range {lo:.0f}..{hi:.0f} m looks implausible")
        else:
            valid = ds.dataset_mask(out_shape=shape) > 0
        cov = 100.0 * valid.mean()
        if cov < 1:
            rec("FAIL", rel, f"essentially empty ({cov:.1f}% valid)")
        elif cov < 95:
            rec("WARN", rel, f"only {cov:.1f}% of pixels valid (source gaps or area outside coverage?)")
        is_cog = ds.tags(ns="IMAGE_STRUCTURE").get("LAYOUT", "").upper() == "COG"
        if rel.endswith("_cog.tif"):
            if not is_cog:
                rec("FAIL", rel, "named _cog.tif but LAYOUT is not COG")
            primary = p.with_name(p.name.replace("_cog.tif", ".tif"))
            if comp == "JPEG" and primary.exists():
                with rasterio.open(primary) as pd:
                    pc = (pd.compression.value if pd.compression else "NONE").upper()
                if pc != "JPEG":
                    rec("WARN", rel, f"COG is lossy JPEG but the primary GeoTIFF is {pc.lower()} — chart text degrades")
        rec("OK", rel, f"{ds.width}x{ds.height} {ds.count}b {ds.dtypes[0]} {comp.lower()} "
                       f"ovr={ovr} res≈{abs(ds.transform.e) * 111320:.2f} m valid={cov:.1f}%"
                       + (" COG" if is_cog else ""))


def check_mbtiles(p: Path, rel: str) -> None:
    with rasterio.open(p) as ds:
        z = ds.tags().get("minzoom"), ds.tags().get("maxzoom")
        if ds.width == 0 or ds.count < 3:
            rec("FAIL", rel, "MBTiles has no raster content")
        else:
            rec("OK", rel, f"{ds.width}x{ds.height} zoom {z[0]}–{z[1]} ovr={len(ds.overviews(1))}")


def check_dted(p: Path, rel: str) -> None:
    level = int(p.suffix[-1])
    with rasterio.open(p) as ds:
        if ds.driver != "DTED":
            rec("FAIL", rel, f"driver {ds.driver}, expected DTED")
            return
        # post-centred cell: left edge = lon - dx/2
        lon = round(ds.bounds.left + ds.transform.a / 2)
        lat = round(ds.bounds.bottom - ds.transform.e / 2)  # e is negative
        exp = dted_expected(level, lat)
        if (ds.width, ds.height) != exp:
            rec("FAIL", rel, f"{ds.width}x{ds.height} posts, expected {exp[0]}x{exp[1]} for level {level} at {lat}°")
        name_ok = p.name.lower() == f"{'n' if lat >= 0 else 's'}{abs(lat):02d}.dt{level}" and \
            p.parent.name.lower() == f"{'e' if lon >= 0 else 'w'}{abs(lon):03d}"
        if not name_ok:
            rec("FAIL", rel, f"path does not match cell SW corner {lat},{lon}")
        a = ds.read(1)
        voids = float((a == -32767).mean() * 100)
        valid = a[a != -32767]
        rng = (int(valid.min()), int(valid.max())) if valid.size else None
        if voids > 50:
            rec("WARN", rel, f"{voids:.1f}% voids")
        if rng and (rng[0] < -500 or rng[1] > 9000):
            rec("WARN", rel, f"elevation range {rng} looks implausible")
        rec("OK", rel, f"DTED{level} {ds.width}x{ds.height} spacing {ds.transform.a * 3600:.1f}\"x"
                       f"{-ds.transform.e * 3600:.1f}\" voids={voids:.1f}% range={rng}")


RASTER_EXT = {".tif", ".tiff", ".dt0", ".dt1", ".dt2", ".ntf", ".nitf", ".jp2", ".img"}


def check_native(p: Path, rel: str, bbox, mode: str) -> None:
    """clipped / original packages: files keep their own projection and format."""
    from rasterio.warp import transform_bounds

    with rasterio.open(p) as ds:
        if ds.crs is None:
            rec("FAIL", rel, "no projection / georeferencing")
            return
        b = transform_bounds(ds.crs, "EPSG:4326", *ds.bounds, densify_pts=21)
        comp = (ds.compression.value if ds.compression else "none").lower()
        desc = (f"{ds.width}x{ds.height} {ds.count}b {ds.dtypes[0]} {comp} {ds.crs.to_string()[:40]}"
                + (" palette" if ds.count == 1 and ds.colorinterp[0].name == "palette" else ""))
    if bbox:
        overlaps = not (b[2] <= bbox[0] or b[0] >= bbox[2] or b[3] <= bbox[1] or b[1] >= bbox[3])
        if not overlaps:
            rec("FAIL", rel, "does not overlap the package area")
            return
        if mode == "clipped":
            # Cut in its own projection, so its lon/lat footprint may bulge a little past the box.
            pad_x, pad_y = (bbox[2] - bbox[0]) * 0.25, (bbox[3] - bbox[1]) * 0.25
            if b[0] < bbox[0] - pad_x or b[2] > bbox[2] + pad_x or b[1] < bbox[1] - pad_y or b[3] > bbox[3] + pad_y:
                rec("FAIL", rel, f"clipped file reaches far outside the area: {tuple(round(v, 4) for v in b)}")
                return
    rec("OK", rel, desc)


def check_checksums(pkg: Path) -> None:
    import hashlib

    sums = pkg / "SHA256SUMS"
    if not sums.exists():
        rec("WARN", "SHA256SUMS", "missing (packages from before checksums were added)")
        return
    bad, n = [], 0
    for line in sums.read_text().splitlines():
        if not line.strip():
            continue
        digest, name = line.split(None, 1)
        name = name.lstrip("*")
        f = pkg / name
        n += 1
        if not f.exists():
            bad.append(f"{name} missing")
            continue
        h = hashlib.sha256()
        with open(f, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        if h.hexdigest() != digest:
            bad.append(f"{name} mismatch")
    if bad:
        rec("FAIL", "SHA256SUMS", "; ".join(bad[:5]) + (" …" if len(bad) > 5 else ""))
    else:
        rec("OK", "SHA256SUMS", f"all {n} file(s) verified")


def thumbnail(p: Path, kind: str, h: int = 360) -> np.ndarray:
    with rasterio.open(p) as ds:
        w = max(1, round(ds.width * h / ds.height))
        if ds.count == 1 and ds.colorinterp[0].name == "palette":
            idx = ds.read(1, out_shape=(h, w), resampling=Resampling.nearest)
            lut = np.zeros((256, 3), np.uint8)
            for k, v in ds.colormap(1).items():
                if 0 <= k < 256:
                    lut[k] = v[:3]
            return np.moveaxis(lut[idx], -1, 0)
        if kind == "elevation" or ds.count == 1:
            z = ds.read(1, out_shape=(h, w), resampling=Resampling.average).astype(np.float64)
            if ds.nodata is not None:
                z[z == ds.nodata] = np.nan
            gy, gx = np.gradient(np.nan_to_num(z, nan=np.nanmean(z) if np.isfinite(z).any() else 0))
            hs = np.clip(128 + (gx - gy) * 8, 0, 255).astype(np.uint8)
            return np.stack([hs] * 3)
        return ds.read([1, 2, 3], out_shape=(3, h, w), resampling=Resampling.average)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("package")
    ap.add_argument("--sheet", help="write a contact-sheet PNG of all GeoTIFF layers here")
    ap.add_argument("--json", action="store_true", help="print results as JSON")
    a = ap.parse_args()
    pkg = Path(a.package)
    man, single = pkg / "manifest.json", pkg / "layer.json"
    if man.exists():
        manifest = json.loads(man.read_text())
        if not (pkg / "README.txt").exists():
            rec("WARN", "README.txt", "missing")
    elif single.exists():  # a single layer folder
        layer = json.loads(single.read_text())
        manifest = {"bbox_wsen": layer.get("bbox"), "layers": [{**layer, "folder": "."}]}
    else:
        print(f"FAIL {pkg}: no manifest.json or layer.json — pass a package or layer folder")
        return 1
    bbox = manifest.get("bbox_wsen")
    mode = manifest.get("mode", "kongsberg")
    if mode != "kongsberg":
        rec("OK", "manifest.json", f"mode: {manifest.get('mode_description', mode)}")
    thumbs = []
    for layer in manifest.get("layers", []):
        if layer.get("status") != "ok":
            rec("WARN", layer.get("folder", layer.get("name", "?")), f"layer {layer.get('status')}: {layer.get('message')}")
            continue
        folder = pkg / layer["folder"]
        for f in layer.get("files", []):
            p = folder / f
            rel = f"{layer['folder']}/{f}"
            if not p.exists():
                rec("FAIL", rel, "listed in manifest but missing")
                continue
            try:
                if mode != "kongsberg":
                    if p.suffix.lower() in RASTER_EXT:
                        check_native(p, rel, bbox, mode)
                        if p.suffix.lower() in (".tif", ".tiff"):
                            thumbs.append((p, layer.get("kind", "rgb")))
                    else:
                        rec("OK", rel, "present (sidecar / metadata)")
                elif p.suffix.lower() == ".tif":
                    check_tif(p, rel, layer, bbox)
                    if not f.endswith("_cog.tif"):
                        thumbs.append((p, layer.get("kind", "rgb")))
                elif p.suffix.lower() == ".mbtiles":
                    check_mbtiles(p, rel)
                elif p.suffix.lower() in (".dt0", ".dt1", ".dt2"):
                    check_dted(p, rel)
                else:
                    rec("OK", rel, "present")
            except Exception as e:  # noqa: BLE001 — report, keep checking the rest
                rec("FAIL", rel, f"could not open: {e}")
    if man.exists():
        check_checksums(pkg)
    if a.sheet and thumbs:
        tiles = [thumbnail(p, k) for p, k in thumbs]
        gap = np.full((3, tiles[0].shape[1], 8), 255, np.uint8)
        parts = []
        for t in tiles:
            parts += [t, gap]
        sheet = np.concatenate(parts[:-1], axis=2)
        with rasterio.open(a.sheet, "w", driver="PNG", width=sheet.shape[2], height=sheet.shape[1],
                           count=3, dtype="uint8") as d:
            d.write(sheet)
        rec("OK", a.sheet, "contact sheet (left→right: " + ", ".join(p.parent.name for p, _ in thumbs) + ")")
    fails = sum(1 for s, _, _ in RESULTS if s == "FAIL")
    warns = sum(1 for s, _, _ in RESULTS if s == "WARN")
    if a.json:
        print(json.dumps({"fails": fails, "warns": warns,
                          "results": [{"status": s, "file": f, "message": m} for s, f, m in RESULTS]}, indent=1))
    else:
        for s, f, m in RESULTS:
            print(f"{s:4s}  {f}: {m}")
        print(f"\n{fails} FAIL, {warns} WARN, {sum(1 for s, _, _ in RESULTS if s == 'OK')} OK")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
