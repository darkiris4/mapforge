"""Maxar Open Data Program: free 30-50 cm satellite imagery for active and recent
disaster-response events (wildfires, floods, earthquakes, hurricanes) — not systematic global
coverage, only wherever an event has been activated.

Public S3 bucket (s3://maxar-opendata), no auth needed. A real STAC catalog three levels deep
(root catalog -> per-event collection -> per-acquisition collection -> per-tile item) links to
per-tile Cloud-Optimized GeoTIFFs, read the same way CopernicusDEM reads AWS Open Data COGs.
Licensed CC BY-NC 4.0 — non-commercial use, attribution required.
See https://registry.opendata.aws/maxar-open-data.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urljoin

import httpx

from .. import speeds
from ..geo import BBox
from .base import Context, Item, Source, _level, cached_note, estimate_result, plain_duration, speed_note

CATALOG_URL = "https://maxar-opendata.s3.amazonaws.com/events/catalog.json"
CATALOG_TTL_S = 6 * 3600
TYPICAL_TILE_BYTES = 20e6  # a ~30-50 cm visual COG tile; used only when the size probe fails
DOWNLOAD_FRACTION = 0.25  # mostly-covered tiles are downloaded whole, slivers read remotely

_lock = threading.Lock()


class MaxarOpenData(Source):
    id = "maxar-open-data"
    name = "Maxar Open Data (disaster events, 30-50 cm)"
    group = "Imagery (public)"
    kind = "rgb"
    access = "public"
    default_res_m = 0.5
    min_res_m = 0.3
    description = ("High-resolution satellite imagery Maxar releases for active and recent "
                   "disaster-response events (wildfires, floods, earthquakes, hurricanes). Only "
                   "covers areas with an activated event, not the whole world — check coverage "
                   "before relying on it.")
    license = ("CC BY-NC 4.0 — non-commercial use only. Requires the attribution \"Maxar Open "
              "Data Program was accessed on <date> from "
              "https://registry.opendata.aws/maxar-open-data\" wherever this imagery is used.")
    category = "imagery"
    plain_name = "High-res disaster imagery — active/recent events only (Maxar)"
    explain = ("30-50 cm satellite photos Maxar releases for specific disaster areas (wildfires, "
              "floods, earthquakes, hurricanes) while they're active or recently active. Sharper "
              "than anything else free here, but only exists where an event has been declared — "
              "use \"Show where data exists\" before relying on it. Non-commercial licence.")

    def detail_levels(self) -> list[dict]:
        return [_level("native", "Full detail (30-50 cm)", self.min_res_m,
                       "As released — the sharpest free imagery this tool ships")]

    def has_coverage(self) -> bool:
        return True

    # -- event discovery (cached; events are added/retired over time, so refreshed on a TTL) --
    def _events_index(self, settings) -> dict:
        """{event_name: {"bbox": [w,s,e,n], "href": "<event>/collection.json"}}"""
        path = settings.cache_dir / "maxar" / "events.json"
        with _lock:
            if path.exists() and time.time() - path.stat().st_mtime <= CATALOG_TTL_S:
                try:
                    return json.loads(path.read_text())
                except ValueError:
                    pass
        ctx = Context(settings)
        with ctx.http(timeout=30) as c:
            catalog = c.get(CATALOG_URL)
            catalog.raise_for_status()
            children = [l["href"] for l in catalog.json().get("links", []) if l.get("rel") == "child"]

        def fetch_event(href):
            url = urljoin(CATALOG_URL, href)
            try:
                with ctx.http(timeout=30) as c:
                    r = c.get(url)
                    r.raise_for_status()
                    col = r.json()
                boxes = col.get("extent", {}).get("spatial", {}).get("bbox") or []
                # Multiple disjoint boxes are common (an event can span separate areas); the
                # overall envelope (first entry, per the STAC spec) is enough for a coverage hint.
                box = boxes[0] if boxes else None
                if not box or len(box) < 4 or any(v is None for v in box[:4]):
                    return None
                name = href.lstrip("./").split("/", 1)[0]
                return name, {"bbox": [box[0], box[1], box[2], box[3]], "href": href}
            except Exception:
                return None

        with ThreadPoolExecutor(16) as pool:
            index = dict(r for r in pool.map(fetch_event, children) if r)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(index))
        return index

    def coverage(self, ctx: Context) -> list[dict]:
        idx = self._events_index(ctx.settings)
        return [{"label": name, "bbox": e["bbox"]} for name, e in idx.items()]

    # -- per-event tile discovery (cached; a published event's tiles don't change) -----------
    def _event_tiles(self, settings, name: str, href: str) -> list[dict]:
        """[{"bbox": [w,s,e,n], "url": <absolute visual COG url>}] for one event, cached."""
        path = settings.cache_dir / "maxar" / "tiles" / f"{name}.json"
        if path.exists():
            try:
                return json.loads(path.read_text())
            except ValueError:
                pass
        event_url = urljoin(CATALOG_URL, href)
        ctx = Context(settings)
        with ctx.http(timeout=30) as c:
            acq_hrefs = [l["href"] for l in c.get(event_url).json().get("links", [])
                        if l.get("rel") == "child"]

            def fetch_items(acq_href):
                acq_url = urljoin(event_url, acq_href)
                try:
                    item_links = [l["href"] for l in c.get(acq_url).json().get("links", [])
                                 if l.get("rel") == "item"]
                except Exception:
                    return []
                out = []
                for item_href in item_links:
                    item_url = urljoin(acq_url, item_href)
                    try:
                        item = c.get(item_url).json()
                        bbox = item.get("bbox")
                        asset = item.get("assets", {}).get("visual")
                        if bbox and asset and asset.get("href"):
                            out.append({"bbox": list(bbox[:4]), "url": urljoin(item_url, asset["href"])})
                    except Exception:
                        continue
                return out

            with ThreadPoolExecutor(8) as pool:
                tiles = [t for batch in pool.map(fetch_items, acq_hrefs) for t in batch]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(tiles))
        return tiles

    def _tiles_for(self, bbox: BBox, settings) -> list[dict]:
        idx = self._events_index(settings)
        events = [(name, e) for name, e in idx.items() if bbox.intersects(BBox(*e["bbox"]))]
        tiles: list[dict] = []
        for name, e in events:
            tiles += self._event_tiles(settings, name, e["href"])
        return [t for t in tiles if bbox.intersects(BBox(*t["bbox"]))]

    # -- estimate / items, following CopernicusDEM's cached-fraction-of-tile pattern ---------
    def _sizes(self, settings, urls: list[str]) -> dict[str, int]:
        path = settings.cache_dir / "maxar" / "sizes.json"
        with _lock:
            sizes = json.loads(path.read_text()) if path.exists() else {}
        missing = [u for u in urls if u not in sizes]
        if missing:
            ctx = Context(settings)

            def head(url):
                try:
                    with ctx.http(timeout=20) as c:
                        r = c.head(url)
                    return url, int(r.headers.get("content-length") or 0) if r.status_code == 200 else None
                except httpx.HTTPError:
                    return url, None

            with ThreadPoolExecutor(16) as pool:
                found = {u: v for u, v in pool.map(head, missing) if v is not None}
            if found:
                with _lock:
                    sizes = json.loads(path.read_text()) if path.exists() else {}
                    sizes.update(found)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(json.dumps(sizes))
        return sizes

    def _local_path(self, settings, url: str) -> Path:
        # The visual COG filename is just "<sensor id>-visual.tif" — identical across every
        # quadkey/date tile from the same acquisition — so the URL's full path (hashed) must be
        # part of the cache key, or different tiles collide onto the same cached file.
        h = hashlib.sha1(url.encode()).hexdigest()[:12]
        return settings.cache_dir / "maxar" / "tif" / f"{h}_{url.rsplit('/', 1)[1]}"

    def estimate(self, bbox: BBox, res_m: float, settings) -> dict:
        tiles = self._tiles_for(bbox, settings)
        if not tiles:
            return estimate_result(cached_pct=100, notes=["No Maxar Open Data event covers this area."],
                                   source_bytes=0, clipped_bytes=0)
        sizes = self._sizes(settings, [t["url"] for t in tiles])
        rate, basis = speeds.get(settings, speeds.host_key("bytes", tiles[0]["url"]))
        todo = total = source = clipped = 0.0
        n_todo = 0
        for t in tiles:
            size = sizes.get(t["url"]) or TYPICAL_TILE_BYTES
            fp = BBox(*t["bbox"])
            inter = bbox.intersection(fp)
            frac = ((inter.east - inter.west) * (inter.north - inter.south)
                    / ((fp.east - fp.west) * (fp.north - fp.south))) if inter else 0.0
            source += size
            clipped += size * frac
            local = self._local_path(settings, t["url"])
            need = size if (local.exists() or frac >= DOWNLOAD_FRACTION) else size * frac * 1.3
            total += need
            if not local.exists():
                todo += need
                n_todo += 1
        cached_pct = 100.0 * (1 - todo / total) if total else 100.0
        seconds = todo / rate
        n = len(tiles)
        notes = [f"{n} tile{'s' if n != 1 else ''} from Maxar Open Data cover this area"
                + (f"; ~{todo / 1e6:.0f} MB to download ({plain_duration(seconds)})." if todo > 0 else ".")]
        notes += cached_note(cached_pct, todo)
        if todo > 0:
            notes += speed_note(basis)
        return estimate_result(todo, cached_pct, n, seconds, notes, basis, source_bytes=source,
                               clipped_bytes=clipped)

    def items(self, bbox: BBox, res_m: float, ctx: Context) -> list[Item]:
        tiles = self._tiles_for(bbox, ctx.settings)
        out = []
        for i, t in enumerate(tiles, 1):
            fp = BBox(*t["bbox"])
            url = t["url"]
            local = self._local_path(ctx.settings, url)
            touched = bbox.intersection(fp)
            frac = ((touched.east - touched.west) * (touched.north - touched.south)
                    / ((fp.east - fp.west) * (fp.north - fp.south))) if touched else 0.0
            ctx.progress(f"{self.name}: tile {i}/{len(tiles)}", (i - 1) / max(len(tiles), 1))
            if local.exists() or frac >= DOWNLOAD_FRACTION:
                path = str(ctx.download(url, local, label=f"Maxar tile {i}/{len(tiles)}"))
            else:
                path = f"/vsicurl/{url}"
            out.append(Item(path=path, footprint=fp, label=url.rsplit("/", 1)[1], native_res_m=self.min_res_m,
                            gdal_env={"GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR"}))
        return out


def maxar_sources() -> list[Source]:
    return [MaxarOpenData()]
