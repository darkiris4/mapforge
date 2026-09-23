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
        for e in self._index(ctx):
            fp = BBox(*e["bbox"])
            if not bbox.intersects(fp):
                continue
            ctx.check()
            zip_name = e["url"].rsplit("/", 1)[1]
            folder = ctx.settings.cache_dir / "faa" / e["edition"]
            tif = folder / Path(e["member"]).name
            if not tif.exists():
                local = ctx.download(e["url"], folder / zip_name, label=zip_name)
                # Extract once: random reads inside a deflated zip member are very slow.
                with zipfile.ZipFile(local) as z:
                    for name in z.namelist():
                        if Path(name).stem == Path(e["member"]).stem:  # .tif plus its .tfw/.htm sidecars
                            target = folder / Path(name).name
                            with z.open(name) as src, open(target.with_name(target.name + ".part"), "wb") as dst:
                                shutil.copyfileobj(src, dst, 1 << 20)
                            target.with_name(target.name + ".part").replace(target)
            out.append(Item(path=str(tif), footprint=fp,
                            label=f"{e['label']} ed. {e['edition']}", native_res_m=e["res_m"]))
        return out


def faa_sources() -> list[Source]:
    return [FaaSource(p) for p in PRODUCTS]
