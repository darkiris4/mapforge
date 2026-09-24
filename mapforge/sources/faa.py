"""FAA Aeronav digital charts (public domain GeoTIFFs, 56-day cycle).

Current editions are discovered by scraping the FAA digital-products pages, so the tool
follows the chart cycle without code changes.  Chart footprints are read remotely through
GDAL's /vsizip//vsicurl/ (only the TIFF header is fetched) and cached per chart edition, so
only the charts that intersect a requested area are ever downloaded.
"""
from __future__ import annotations

import json
import re
import shutil
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import rasterio
from rasterio.warp import transform_bounds

from ..geo import BBox
from .base import Context, Item, Source
from .ziputil import list_remote_zip, read_remote_member

PAGES = {
    "vfr": "https://www.faa.gov/air_traffic/flight_info/aeronav/digital_products/vfr/",
    "ifr": "https://www.faa.gov/air_traffic/flight_info/aeronav/digital_products/ifr/",
}
LINK_RE = re.compile(r"https://aeronav\.faa\.gov/(?:visual|enroute)/(\d\d-\d\d-\d{4})/[^\s>\"']+?\.zip", re.I)
PAGE_TTL_S = 6 * 3600

_lock = threading.Lock()
_file_locks: dict[str, threading.Lock] = {}


def _file_lock(path) -> threading.Lock:
    with _lock:
        return _file_locks.setdefault(str(path), threading.Lock())


@dataclass
class FaaProduct:
    id: str
    name: str
    page: str
    zip_re: str  # matched against the URL path after the edition date
    member_re: str  # matched against the .tif name inside the zip
    default_res_m: float
    description: str


PRODUCTS = [
    FaaProduct("faa-sectional", "VFR Sectional (1:500k)", "vfr", r"^sectional-files/", r" SEC\.tif$", 42,
               "Primary VFR navigation charts for the US."),
    FaaProduct("faa-tac", "VFR Terminal Area (1:250k)", "vfr", r"^tac-files/", r" TAC\.tif$", 21,
               "Terminal Area Charts around Class B airspace."),
    FaaProduct("faa-flyway", "VFR Flyway Planning", "vfr", r"^tac-files/", r" FLY\.tif$", 21,
               "Flyway planning charts printed on the back of TACs."),
    FaaProduct("faa-heli", "Helicopter Route Charts", "vfr", r"^heli_files/", r"\.tif$", 10,
               "Helicopter route charts for major metro areas."),
    FaaProduct("faa-caribbean", "VFR Caribbean", "vfr", r"^Caribbean/", r"\.tif$", 42,
               "Caribbean VFR aeronautical charts."),
    FaaProduct("faa-grand-canyon", "VFR Grand Canyon", "vfr", r"^grand_canyon_files/", r"\.tif$", 21,
               "Grand Canyon VFR chart."),
    FaaProduct("faa-ifr-low", "IFR Enroute Low", "ifr", r"^(enr_l\d+|DELCB\w*_tif|DELCBA\w*_tif)\.zip$",
               r"^ENR_C?L\w*\.tif$", 60, "IFR enroute low altitude charts (CONUS + Caribbean)."),
    FaaProduct("faa-ifr-high", "IFR Enroute High", "ifr", r"^(enr_h\d+|DEHCB\w*_tif)\.zip$", r"^ENR_C?H\w*\.tif$", 120,
               "IFR enroute high altitude charts."),
    FaaProduct("faa-ifr-area", "IFR Area Charts", "ifr", r"^enr_a\d+\.zip$", r"\.tif$", 25,
               "IFR area charts for terminal areas."),
    FaaProduct("faa-ifr-alaska", "IFR Enroute Alaska", "ifr", r"^enr_ak[lh]\d+\.zip$", r"\.tif$", 60,
               "Alaska IFR enroute low/high charts."),
    FaaProduct("faa-ifr-pacific", "IFR Enroute Pacific", "ifr", r"^enr_p\d+\.zip$", r"\.tif$", 60,
               "Pacific IFR enroute charts."),
    FaaProduct("faa-ifr-oceanic", "IFR Oceanic Route Charts", "ifr", r"^(narc|porc|watrs)_tif\.zip$", r"\.tif$", 300,
               "North Atlantic, North Pacific and West Atlantic route charts."),
]


def _gdal_remote_env(settings) -> dict:
    return {
        "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
        "GDAL_HTTP_USERAGENT": settings.user_agent,
        "GDAL_HTTP_MAX_RETRY": "3",
        "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".zip",
    }


def _fgdc_bounds(text: str) -> list[float] | None:
    vals = {}
    for k in ("West", "East", "North", "South"):
        m = re.search(k + r"_Bounding_Coordinate:\s*(?:<[^>]*>\s*)*(-?\d+(?:\.\d+)?)", text)
        if not m:
            return None
        vals[k] = float(m.group(1))
    if not (vals["West"] < vals["East"] and vals["South"] < vals["North"]):
        return None
    return [vals["West"], vals["South"], vals["East"], vals["North"]]


class FaaSource(Source):
    group = "Aeronautical — FAA (public)"
    kind = "rgb"
    access = "public"
    license = "US Government work, public domain. Not for navigation once the edition expires."
    resampling = "nearest"

    def __init__(self, product: FaaProduct):
        self.p = product
        self.id = product.id
        self.name = product.name
        self.description = product.description
        self.default_res_m = product.default_res_m
        self.min_res_m = product.default_res_m / 2

    def has_coverage(self) -> bool:
        return True

    # -- discovery -----------------------------------------------------------------
    def _page_links(self, ctx: Context) -> list[tuple[str, str, str]]:
        """[(url, edition, path_after_date)] for this product's page."""
        cache = ctx.settings.cache_dir / "faa" / f"page_{self.p.page}.html"
        cache.parent.mkdir(parents=True, exist_ok=True)
        with _lock:
            if not cache.exists() or time.time() - cache.stat().st_mtime > PAGE_TTL_S:
                try:
                    with ctx.http() as c:
                        r = c.get(PAGES[self.p.page])
                        r.raise_for_status()
                        cache.write_text(r.text)
                except Exception:
                    if not cache.exists():
                        raise
        html = cache.read_text()
        seen, out = set(), []
        for m in LINK_RE.finditer(html):
            url, edition = m.group(0), m.group(1)
            rest = url.split(f"/{edition}/", 1)[1]
            if url in seen or not re.search(self.p.zip_re, rest):
                continue
            seen.add(url)
            out.append((url, edition, rest))
        return out

    def _index(self, ctx: Context) -> list[dict]:
        idx_path = ctx.settings.cache_dir / "faa" / f"index_{self.id}.json"
        idx: dict = json.loads(idx_path.read_text()) if idx_path.exists() else {}
        links = self._page_links(ctx)
        missing = [link for link in links if link[0] not in idx]
        if missing:
            ctx.progress(f"Indexing {len(missing)} {self.name} chart(s) (one-time per edition)", None)
            env = _gdal_remote_env(ctx.settings)

            def probe(link):
                url, edition, _ = link
                entries = []
                with ctx.http() as c:
                    members = list_remote_zip(c, url)
                    tifs = [m for m in members if re.search(self.p.member_re, m.name.split("/")[-1], re.I)]
                    for m in tifs:
                        stem = Path(m.name).stem
                        # Fast path: bounds from the FGDC metadata (.htm) shipped beside each chart.
                        bbox = None
                        htm = next((h for h in members if h.name.lower().endswith(".htm")
                                    and Path(h.name).stem.startswith(stem)), None)
                        if htm:
                            try:
                                bbox = _fgdc_bounds(read_remote_member(c, url, htm).decode("latin1"))
                            except Exception:
                                bbox = None
                        res = self.p.default_res_m
                        if bbox is None:  # slow path: read the GeoTIFF header remotely
                            with rasterio.Env(**env), rasterio.open(f"/vsizip//vsicurl/{url}/{m.name}") as ds:
                                bbox = list(transform_bounds(ds.crs, "EPSG:4326", *ds.bounds, densify_pts=64))
                                res = abs(ds.transform.a) if ds.crs.is_projected else abs(ds.transform.a) * 111_320
                        entries.append({"member": m.name, "bbox": bbox, "edition": edition,
                                        "res_m": round(res, 2), "label": stem})
                return url, entries

            def safe_probe(link):
                try:
                    return probe(link)
                except Exception as e:  # one unreadable chart must not sink the product
                    ctx.progress(f"Skipping {link[0].rsplit('/', 1)[1]}: {e}", None)
                    return link[0], None

            with ThreadPoolExecutor(12) as pool:
                for url, entries in pool.map(safe_probe, missing):
                    if entries is not None:
                        idx[url] = entries
            live = {link[0] for link in links}
            idx = {u: e for u, e in idx.items() if u in live}  # drop superseded editions
            idx_path.write_text(json.dumps(idx, indent=1))
        return [dict(e, url=u) for u, es in idx.items() for e in es]

    def coverage(self, ctx: Context) -> list[dict]:
        return [{"label": f"{e['label']} ({e['edition']})", "bbox": e["bbox"]} for e in self._index(ctx)]

    def items(self, bbox: BBox, res_m: float, ctx: Context) -> list[Item]:
        out = []
        wanted = [e for e in self._index(ctx) if bbox.intersects(BBox(*e["bbox"]))]
        for n, e in enumerate(wanted):
            fp = BBox(*e["bbox"])
            ctx.check()
            ctx.progress(f"{self.name}: chart {n + 1}/{len(wanted)} ({e['label']})", n / len(wanted))
            zip_name = e["url"].rsplit("/", 1)[1]
            folder = ctx.settings.cache_dir / "faa" / e["edition"]
            tif = folder / Path(e["member"]).name
            # Two jobs needing the same chart must not download/extract it concurrently.
            with _file_lock(tif):
                if not tif.exists():
                    local = ctx.download(e["url"], folder / zip_name, label=zip_name)
                    # Extract once: random reads inside a deflated zip member are very slow.
                    with zipfile.ZipFile(local) as z:
                        for name in z.namelist():
                            if Path(name).stem == Path(e["member"]).stem:  # .tif plus .tfw/.htm sidecars
                                target = folder / Path(name).name
                                part = target.with_name(target.name + ".part")
                                with z.open(name) as src, open(part, "wb") as dst:
                                    shutil.copyfileobj(src, dst, 1 << 20)
                                part.replace(target)
            clip = e.get("clip")
            if not clip or clip.get("v") != CLIP_VERSION:
                ctx.progress(f"Finding chart neatline: {e['label']}", None)
                try:
                    clip = detect_clip(tif)
                except Exception:
                    clip = {"v": CLIP_VERSION, "px": None, "ll": [None] * 4}
                self._store_clip(ctx, e["url"], e["member"], clip)
            out.append(Item(path=str(tif), footprint=fp, clip=clip_polygons(tif, clip),
                            label=f"{e['label']} ed. {e['edition']}", native_res_m=e["res_m"]))
        return out

    def _store_clip(self, ctx: Context, url: str, member: str, clip: dict) -> None:
        idx_path = ctx.settings.cache_dir / "faa" / f"index_{self.id}.json"
        with _lock:
            idx = json.loads(idx_path.read_text()) if idx_path.exists() else {}
            for entry in idx.get(url, []):
                if entry["member"] == member:
                    entry["clip"] = clip
            idx_path.write_text(json.dumps(idx, indent=1))


# --------------------------------------------------------------------------------------
# Neatline / collar detection
# --------------------------------------------------------------------------------------
# FAA chart GeoTIFFs are the whole printed sheet: the chart face plus a white collar carrying
# the legend, title and scale bars (on sectionals: west + south; TACs: mostly west).  Where
# neighbouring charts overlap, the collar of one would otherwise paint over the face of the
# other.  Two independent detectors find the face; the mosaic uses the intersection:
#
#   ll  - the neatline as a parallel/meridian: the outermost whole-minute line that is dark
#         along its whole central span AND has white paper just outside it.  Exact for
#         sectionals, whose neatlines are graticule lines.
#   px  - a pixel-space cut: rows/columns at the sheet edge that are mostly white paper,
#         followed by rows/columns that are almost entirely chart face.  Catches collars whose
#         border is not on a whole minute (TACs) and thin paper margins.
#
# A side is only clipped when its evidence is unambiguous; otherwise it is left alone and
# the mosaic falls back to the "deepest inside wins" rule.  White-background charts (most
# helicopter charts) therefore stay unclipped rather than being cut wrongly.
CLIP_VERSION = 1
_DARK, _WHITE = 90.0, 240


def detect_clip(path) -> dict:
    import math

    import numpy as np
    from rasterio.warp import transform as warp_transform

    with rasterio.open(path) as ds:
        if ds.count != 1 or ds.colorinterp[0].name != "palette":
            return {"v": CLIP_VERSION, "px": None, "ll": [None] * 4}
        a = ds.read(1)
        cmap = ds.colormap(1)
        crs, T, H, W = ds.crs, ds.transform, ds.height, ds.width
    lum = np.zeros(256, np.float32)
    white = np.zeros(256, bool)
    for k, v in cmap.items():
        if 0 <= k < 256:
            lum[k] = 0.299 * v[0] + 0.587 * v[1] + 0.114 * v[2]
            white[k] = min(v[:3]) >= _WHITE

    # -- pixel-space cut ------------------------------------------------------------------
    dec = max(1, min(H, W) // 3000)
    wm = white[a[::dec, ::dec]]
    h, w = wm.shape
    col = wm[int(h * 0.2):int(h * 0.8), :].mean(axis=0)
    row = wm[:, int(w * 0.2):int(w * 0.8)].mean(axis=1)

    def cut(frac) -> int:
        n = len(frac)
        k = max(3, int(n * 0.02))
        for i in range(int(n * 0.3)):
            if frac[i:i + k].max() < 0.3:
                if i == 0:
                    return 0
                collar, face = frac[:i].mean(), frac[i:i + max(k, int(n * 0.1))].mean()
                return i if collar >= 0.6 and face <= 0.1 else 0
        return 0

    c0, c1 = cut(col) * dec, W - cut(col[::-1]) * dec
    r0, r1 = cut(row) * dec, H - cut(row[::-1]) * dec
    px = [c0, r0, c1, r1] if (c0, r0, c1, r1) != (0, 0, W, H) else None

    # -- lon/lat neatlines ----------------------------------------------------------------
    inv = ~T

    def sample(lons, lats, rad):
        xs, ys = warp_transform("EPSG:4326", crs, lons, lats)
        c, r = inv @ (np.asarray(xs), np.asarray(ys))
        c, r = np.round(c).astype(int), np.round(r).astype(int)
        ok = (c >= rad) & (c < W - rad) & (r >= rad) & (r < H - rad)
        return c, r, ok

    def dark_frac(lons, lats, rad=2):
        c, r, ok = sample(lons, lats, rad)
        if ok.mean() < 0.9:
            return 0.0
        c, r = c[ok], r[ok]
        d = np.zeros(len(c), bool)
        for dy in range(-rad, rad + 1):
            for dx in range(-rad, rad + 1):
                d |= lum[a[r + dy, c + dx]] < _DARK
        return float(d.mean())

    def white_frac(lons, lats):
        c, r, ok = sample(lons, lats, 0)
        if ok.mean() < 0.5:
            return 0.0  # probing off the sheet proves nothing
        return float(white[a[r[ok], c[ok]]].mean())

    xs, ys = T @ (np.array([0, W / 2, W, W / 2]), np.array([H / 2, 0, H / 2, H]))
    lo, la = warp_transform(crs, "EPSG:4326", xs, ys)
    wm_, nm_, em_, sm_ = lo[0], la[1], lo[2], la[3]
    n = 500
    lat_span = np.linspace(sm_ + 0.15 * (nm_ - sm_), nm_ - 0.15 * (nm_ - sm_), n)
    lon_span = np.linspace(wm_ + 0.15 * (em_ - wm_), em_ - 0.15 * (em_ - wm_), n)
    probe = 2 / 60
    limit = int(min(90, 60 * 0.3 * min(nm_ - sm_, em_ - wm_)))  # search at most 30% inward
    found: list[float | None] = []
    for side in "WSEN":
        hit = None
        for k in range(limit):
            if side == "W":
                v = math.ceil(wm_ * 60) / 60 + k / 60
                ok = dark_frac(np.full(n, v), lat_span) >= 0.95 and white_frac(np.full(n, v - probe), lat_span) >= 0.8
            elif side == "E":
                v = math.floor(em_ * 60) / 60 - k / 60
                ok = dark_frac(np.full(n, v), lat_span) >= 0.95 and white_frac(np.full(n, v + probe), lat_span) >= 0.8
            elif side == "S":
                v = math.ceil(sm_ * 60) / 60 + k / 60
                ok = dark_frac(lon_span, np.full(n, v)) >= 0.95 and white_frac(lon_span, np.full(n, v - probe)) >= 0.8
            else:
                v = math.floor(nm_ * 60) / 60 - k / 60
                ok = dark_frac(lon_span, np.full(n, v)) >= 0.95 and white_frac(lon_span, np.full(n, v + probe)) >= 0.8
            if ok:
                hit = round(v, 6)
                break
        found.append(hit)
    return {"v": CLIP_VERSION, "px": px, "ll": found}


def clip_polygons(path, clip: dict | None) -> list[list[tuple[float, float]]] | None:
    """Turn a detect_clip() result into lon/lat polygons for Item.clip."""
    if not clip:
        return None
    polys = []
    w, s, e, n = clip.get("ll") or [None] * 4
    if any(v is not None for v in (w, s, e, n)):
        w, s = (-180.0 if w is None else w), (-90.0 if s is None else s)
        e, n = (180.0 if e is None else e), (90.0 if n is None else n)
        polys.append([(w, s), (e, s), (e, n), (w, n), (w, s)])
    if clip.get("px"):
        import numpy as np
        from rasterio.warp import transform as warp_transform

        c0, r0, c1, r1 = clip["px"]
        t = np.linspace(0, 1, 65)
        cs = np.concatenate([c0 + (c1 - c0) * t, np.full(65, c1), c1 - (c1 - c0) * t, np.full(65, c0)])
        rs = np.concatenate([np.full(65, r0), r0 + (r1 - r0) * t, np.full(65, r1), r1 - (r1 - r0) * t])
        with rasterio.open(path) as ds:
            xs, ys = ds.transform @ (cs, rs)
            lons, lats = warp_transform(ds.crs, "EPSG:4326", xs, ys)
        polys.append(list(zip(lons, lats)))
    return polys or None


def faa_sources() -> list[Source]:
    return [FaaSource(p) for p in PRODUCTS]
