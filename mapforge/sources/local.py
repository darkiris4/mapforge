"""Local library: NGA and other products already on disk.

Drop NGA media (CADRG/CIB RPF trees with an A.TOC, ECRG with TOC.xml, DTED cells, NITF,
GeoTIFF, JPEG 2000 ...) into any configured library directory.  Scanning groups them into
products (e.g. "CADRG ONC 1:1M", "CIB 5M", "DTED Level 2") that behave exactly like the
online sources: bound, mosaic and convert.
"""
from __future__ import annotations

import os
import re
import threading
import time
from pathlib import Path

import rasterio
from rasterio.warp import transform_bounds

from ..geo import BBox
from .base import Context, Item, Source, _level, estimate_result

RASTER_EXT = {".tif", ".tiff", ".ntf", ".nitf", ".nsf", ".jp2", ".img", ".vrt", ".sid", ".ecw"}
DTED_RE = re.compile(r"\.dt([012])$", re.I)
INDEX = "library_index.json"
_scan_lock = threading.Lock()


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def _describe(path: str) -> dict | None:
    try:
        with rasterio.open(path) as ds:
            if ds.crs is None:
                return None
            b = transform_bounds(ds.crs, "EPSG:4326", *ds.bounds, densify_pts=21)
            res = abs(ds.transform.a) * (1 if ds.crs.is_projected else 111_320)
            single = ds.count == 1 and ds.colorinterp[0].name not in ("palette",)
            elev = single and ds.dtypes[0] in ("int16", "float32", "float64", "int32")
            return {"bbox": [round(v, 8) for v in b], "res_m": round(res, 3), "kind": "elevation" if elev else "rgb"}
    except Exception:
        return None


def _rpf_product(sub: str) -> str:
    # "NITF_TOC_ENTRY:CADRG_ONC_1:1M_1_1:/x/A.TOC" -> "CADRG ONC 1:1M"
    body = sub.split(":", 1)[1].rsplit(":", 1)[0] if sub.startswith("NITF_TOC_ENTRY:") else sub
    body = re.sub(r"(_\d+)+$", "", body)
    return body.replace("_", " ")


def _ecrg_product(sub: str) -> str:
    # "ECRG_TOC_ENTRY:ProductTitle:DiscId:Scale:/x/TOC.xml"
    parts = sub.split(":")
    return "ECRG " + " ".join(p for p in parts[1:-1] if p and not p.startswith("/"))


def scan(settings, progress=lambda m: None) -> dict:
    """Walk the library directories and rebuild the product index."""
    with _scan_lock:
        products: dict[str, dict] = {}

        def add(product: str, path: str, info: dict, root: Path):
            key = _slug(product)
            p = products.setdefault(key, {"id": f"local-{key}", "name": product, "kind": info["kind"],
                                          "root": str(root), "items": []})
            p["items"].append({"path": path, **info})

        for root in settings.library_dirs:
            if not root.exists():
                continue
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                lower = {f.lower(): f for f in filenames}
                d = Path(dirpath)
                if "a.toc" in lower:
                    toc = str(d / lower["a.toc"])
                    progress(f"RPF {toc}")
                    try:
                        with rasterio.open(toc) as ds:
                            subs = ds.subdatasets or [toc]
                    except Exception:
                        subs = []
                    for sub in subs:
                        info = _describe(sub)
                        if info:
                            add(_rpf_product(sub), sub, info, root)
                    dirnames[:] = []  # frame files below an A.TOC are covered by the TOC
                    continue
                if "toc.xml" in lower:
                    toc = str(d / lower["toc.xml"])
                    progress(f"ECRG {toc}")
                    try:
                        with rasterio.open(toc) as ds:
                            subs = ds.subdatasets or [toc]
                        for sub in subs:
                            info = _describe(sub)
                            if info:
                                add(_ecrg_product(sub), sub, info, root)
                        dirnames[:] = []
                        continue
                    except Exception:
                        pass  # some other TOC.xml; keep walking
                for f in filenames:
                    full = d / f
                    m = DTED_RE.search(f)
                    if m:
                        info = _describe(str(full))
                        if info:
                            info["kind"] = "elevation"
                            add(f"DTED Level {m.group(1)}", str(full), info, root)
                    elif full.suffix.lower() in RASTER_EXT:
                        info = _describe(str(full))
                        if info:
                            rel = full.relative_to(root).parts
                            folder = rel[0] if len(rel) > 1 else "Loose files"
                            add(f"{folder} ({'elevation' if info['kind'] == 'elevation' else 'imagery'})",
                                str(full), info, root)
        index = {"scanned_at": time.time(), "dirs": [str(p) for p in settings.library_dirs],
                 "products": sorted(products.values(), key=lambda p: p["name"])}
        settings.save_json(INDEX, index)
        return index


class LocalProduct(Source):
    group = "Local library — NGA / on-disk"
    access = "local"

    def __init__(self, product: dict):
        self.p = product
        self.id = product["id"]
        self.name = product["name"]
        self.kind = product["kind"]
        n = len(product["items"])
        self.description = f"{n} file(s) under {product['root']}"
        self.license = "Observe the distribution statement / handling caveats on the source media."
        res = sorted(i["res_m"] for i in product["items"])
        self.default_res_m = round(res[len(res) // 2], 2) if res else 30.0
        self.min_res_m = round(res[0] / 2, 2) if res else 0.5
        name = self.name.upper()
        if "CADRG" in name or "ECRG" in name or "CIB" in name:
            self.resampling = "nearest" if "CIB" not in name else "bilinear"
        self.category = "local"
        self.plain_name = self.name
        self.explain = _explain(name, self.kind, n, product["root"])

    def detail_levels(self) -> list[dict]:
        d = self.default_res_m
        return [_level("overview", "Lighter download", d * 4, "A quarter of the detail in each direction"),
                _level("native", "Full detail", d, "The files' own resolution")]

    def has_coverage(self) -> bool:
        return True

    def coverage(self, ctx: Context) -> list[dict]:
        return [{"label": Path(i["path"].rsplit(":", 1)[-1]).name, "bbox": i["bbox"]} for i in self.p["items"]]

    def items(self, bbox: BBox, res_m: float, ctx: Context) -> list[Item]:
        return [Item(path=i["path"], footprint=BBox(*i["bbox"]), label=Path(i["path"]).name, native_res_m=i["res_m"])
                for i in self.p["items"] if bbox.intersects(BBox(*i["bbox"]))]

    def estimate(self, bbox: BBox, res_m: float, settings) -> dict:
        hits = [i for i in self.p["items"] if bbox.intersects(BBox(*i["bbox"]))]
        if not hits:
            return estimate_result(cached_pct=100, notes=["None of these files cover this area."],
                                   source_bytes=0, clipped_bytes=0)
        source = clipped = 0.0
        known = True
        for i in hits:
            size = _file_size(i["path"])
            if size is None:
                known = False
                continue
            fp = BBox(*i["bbox"])
            inter = bbox.intersection(fp)
            frac = ((inter.east - inter.west) * (inter.north - inter.south)
                    / max((fp.east - fp.west) * (fp.north - fp.south), 1e-12)) if inter else 0.0
            source += size
            clipped += size * frac
        n = len(hits)
        notes = [f"Already on this server ({n} file{'s' if n != 1 else ''} cover this area) — no download."]
        return estimate_result(0, 100, 0, 0, notes, "measured",
                               source_bytes=source if known else None, clipped_bytes=clipped if known else None)


def _explain(upper_name: str, kind: str, n: int, root: str) -> str:
    what = ("NGA scanned aeronautical charts (CADRG)" if "CADRG" in upper_name
            else "NGA enhanced scanned charts (ECRG)" if "ECRG" in upper_name
            else "NGA controlled image base — grey-scale satellite imagery (CIB)" if "CIB" in upper_name
            else "Elevation cells in the standard military DTED format" if "DTED" in upper_name
            else "Elevation files" if kind == "elevation" else "Map or imagery files")
    return f"{what}: {n} file(s) already on this server under {root}."


def _file_size(path: str) -> int | None:
    """Size of a library item on disk (None for subdatasets we can't size cheaply, e.g. RPF TOC entries)."""
    p = Path(path)
    if p.is_file():
        return p.stat().st_size
    return None


def local_sources(settings) -> list[Source]:
    index = settings.load_json(INDEX, None)
    if index is None:
        index = scan(settings)
    return [LocalProduct(p) for p in index["products"]]
