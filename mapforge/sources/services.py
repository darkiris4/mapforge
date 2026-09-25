"""Service-backed sources: cloud-optimised files, ArcGIS ImageServers, XYZ/WMTS/WMS.

These classes back both the built-in public sources and user-defined endpoints (which is
how PKI-protected services such as NGA GEGD are added — see sources/custom.py).
"""
from __future__ import annotations

import hashlib
import json
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from pathlib import Path
from xml.sax.saxutils import escape

import httpx

from .. import speeds
from ..geo import BBox, meters_to_deg, web_mercator_res, web_mercator_zoom_for
from . import xyz
from .base import (Auth, Cancelled, Context, Item, Source, TooManyTiles, _level, cached_note, estimate_result,
                   generic_detail_levels, plain_duration, speed_note)

MERC = 20037508.342789244
TILE_CONNECTIONS = 8  # parallel tile requests GDAL makes (MaxConnections in _xyz_xml)


# --------------------------------------------------------------------------------------
# Copernicus DEM (global elevation, COGs on AWS Open Data)
# --------------------------------------------------------------------------------------
class CopernicusDEM(Source):
    group = "Elevation (public)"
    kind = "elevation"
    access = "public"
    license = "Copernicus DEM © DLR e.V. / Airbus, provided under COPERNICUS by the EU and ESA; free incl. commercial use."
    TYPICAL_TILE_BYTES = 15e6  # used only when the size probe fails

    def __init__(self, arcsec: int):
        self.arcsec = arcsec
        self.id = f"copernicus-dem-{arcsec}"
        self.name = f"Copernicus DEM GLO-{arcsec} ({'~30' if arcsec == 30 else '~90'} m)"
        self.description = "Global digital surface model. Exports cleanly to DTED."
        self.default_res_m = 30.0 if arcsec == 30 else 90.0
        self.min_res_m = self.default_res_m
        self.bucket = "copernicus-dem-30m" if arcsec == 30 else "copernicus-dem-90m"
        # Tiles at least this fraction covered by the area are downloaded and cached; less
        # than that are read remotely (cloud-optimised GeoTIFF window reads).
        self.DOWNLOAD_FRACTION = 0.25
        self.code = "10" if arcsec == 30 else "30"
        self.category = "elevation"
        self.plain_name = f"Terrain elevation — worldwide ({'30' if arcsec == 30 else '90'} m)"
        self.explain = ("Height of the ground (including buildings and trees) anywhere on Earth. Use it for "
                        "terrain shading and to produce DTED files.")

    def detail_levels(self) -> list[dict]:
        if self.arcsec == 30:
            return [_level("overview", "Coarse terrain", 90, "About DTED Level 1 spacing; small download"),
                    _level("native", "Full detail (30 m)", 30, "About DTED Level 2 spacing")]
        return [_level("native", "Full detail (90 m)", 90, "About DTED Level 1 spacing")]

    def _tile_sizes(self, settings, cells: list[tuple[int, int]]) -> dict[str, int]:
        """{"lat,lon": bytes} for tiles that exist (0 = no tile: ocean). Cached on disk."""
        path = settings.cache_dir / "dem" / self.id / "tiles.json"
        known: dict[str, int] = json.loads(path.read_text()) if path.exists() else {}
        missing = [c for c in cells if f"{c[0]},{c[1]}" not in known]
        if missing:
            ctx = Context(settings)

            def head(cell):
                try:
                    with ctx.http(timeout=20) as c:
                        r = c.head(self.tile_url(*cell))
                    return cell, int(r.headers.get("content-length") or 0) if r.status_code == 200 else 0
                except httpx.HTTPError:
                    return cell, None  # unknown: don't cache

            with ThreadPoolExecutor(16) as pool:
                for cell, size in pool.map(head, missing):
                    if size is not None:
                        known[f"{cell[0]},{cell[1]}"] = size
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(known))
        return known

    def estimate(self, bbox: BBox, res_m: float, settings) -> dict:
        cells = [(lat, lon) for lat in range(math.floor(bbox.south), math.ceil(bbox.north))
                 for lon in range(math.floor(bbox.west), math.ceil(bbox.east))]
        if len(cells) > 400:
            return estimate_result(notes=["This area spans more than 400 one-degree tiles — split it into smaller jobs."])
        sizes = self._tile_sizes(settings, cells)
        rate, basis = speeds.get(settings, speeds.host_key("bytes", self.tile_url(0, 0)))
        cache = settings.cache_dir / "dem" / self.id
        todo = total = source = clipped = 0.0  # total = bytes this area needs, cached or not
        tiles = partial = 0
        for lat, lon in cells:
            size = sizes.get(f"{lat},{lon}")
            if size is None:
                size = self.TYPICAL_TILE_BYTES
            if not size:
                continue  # ocean: no tile
            tiles += 1
            fp = BBox(lon, lat, lon + 1, lat + 1)
            inter = bbox.intersection(fp)
            frac = ((inter.east - inter.west) * (inter.north - inter.south)) if inter else 0.0
            source += size
            clipped += size * frac
            local = cache / self.tile_url(lat, lon).rsplit("/", 1)[1]
            # Mostly-covered tiles are downloaded whole; slivers are windowed reads of the
            # cloud-optimised file (+ overhead) — the same rule items() applies.
            need = size if (local.exists() or frac >= self.DOWNLOAD_FRACTION) else size * frac * 1.3
            total += need
            if local.exists():
                continue
            if frac < self.DOWNLOAD_FRACTION:
                partial += 1
            todo += need
        if not tiles:
            return estimate_result(cached_pct=100, notes=["No elevation tiles here (open ocean)."],
                                   source_bytes=0, clipped_bytes=0)
        cached_pct = 100.0 * (1 - todo / total) if total else 100.0
        seconds = todo / rate
        notes = [f"{tiles} elevation tile{'s (1° × 1°) cover' if tiles != 1 else ' (1° × 1°) covers'} this area"
                 + (f"; ~{todo / 1e6:.0f} MB to download ({plain_duration(seconds)})." if todo > 0 else ".")]
        if partial:
            notes.append(f"Only the needed part of {partial} tile{'s' if partial != 1 else ''} is read, "
                         "not the whole file.")
        notes += cached_note(cached_pct, todo)
        if todo > 0:
            notes += speed_note(basis)
        return estimate_result(todo, cached_pct, tiles, seconds, notes, basis, source_bytes=source,
                               clipped_bytes=clipped)

    def tile_url(self, lat: int, lon: int) -> str:
        ns = f"{'N' if lat >= 0 else 'S'}{abs(lat):02d}_00"
        ew = f"{'E' if lon >= 0 else 'W'}{abs(lon):03d}_00"
        name = f"Copernicus_DSM_COG_{self.code}_{ns}_{ew}_DEM"
        return f"https://{self.bucket}.s3.amazonaws.com/{name}/{name}.tif"

    def items(self, bbox: BBox, res_m: float, ctx: Context) -> list[Item]:
        cells = [(lat, lon) for lat in range(math.floor(bbox.south), math.ceil(bbox.north))
                 for lon in range(math.floor(bbox.west), math.ceil(bbox.east))]
        if len(cells) > 400:
            raise ValueError("Area spans more than 400 one-degree DEM tiles; split it into smaller jobs")

        def exists(cell):
            with ctx.http(timeout=30) as c:
                return cell, c.head(self.tile_url(*cell)).status_code == 200

        with ThreadPoolExecutor(16) as pool:
            present = [cell for cell, ok in pool.map(exists, cells) if ok]  # ocean tiles do not exist
        cache = ctx.settings.cache_dir / "dem" / self.id
        out = []
        for i, (lat, lon) in enumerate(sorted(present), 1):
            url = self.tile_url(lat, lon)
            fp = BBox(lon, lat, lon + 1, lat + 1)
            local = cache / url.rsplit("/", 1)[1]
            touched = bbox.intersection(fp)
            frac = ((touched.east - touched.west) * (touched.north - touched.south)) if touched else 0.0
            ctx.progress(f"{self.name}: tile {i}/{len(present)}", (i - 1) / max(len(present), 1))
            if local.exists() or frac >= self.DOWNLOAD_FRACTION:
                # Mostly-covered tiles are fetched once and cached for later jobs.
                path = str(ctx.download(url, local, label=f"DEM tile {i}/{len(present)}"))
            else:
                # Small overlap: read just the needed window of the COG over HTTP.
                path = f"/vsicurl/{url}"
            out.append(Item(path=path, footprint=fp, label=url.rsplit("/", 1)[1], native_res_m=self.default_res_m,
                            gdal_env={"GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR"}))
        return out


# --------------------------------------------------------------------------------------
# ArcGIS ImageServer exportImage (USGS NAIP, USGS 3DEP, many agency servers)
# --------------------------------------------------------------------------------------
class ArcGISImageServer(Source):
    access = "public"
    WORKERS = 4
    MIN_SPLIT = 500  # px: smallest export edge worth splitting down to
    RETRY_DELAYS_S = (5, 15, 45)  # back-off between attempts at the same export
    CHUNK = 2000  # px per request edge; servers cap at ~4000 and time out on big exports

    def __init__(self, id, name, url, group, kind="rgb", band_ids="0,1,2", extent=None, description="",
                 license="", default_res_m=1.0, min_res_m=0.3, auth: Auth | None = None, access="public",
                 levels: list[dict] | None = None):
        self.id, self.name, self.url, self.group, self.kind = id, name, url.rstrip("/"), group, kind
        self.band_ids, self.extent, self.description, self.license = band_ids, extent, description, license
        self.default_res_m, self.min_res_m, self.auth, self.access = default_res_m, min_res_m, auth or Auth(), access
        self.levels = levels

    def detail_levels(self) -> list[dict]:
        return self.levels or generic_detail_levels(self.kind, self.default_res_m, self.min_res_m)

    def has_coverage(self) -> bool:
        # A source limited to a known rectangle (e.g. NAIP's lower-48 extent) has real coverage
        # to show; one with no extent covers wherever the server answers, which isn't worth a box.
        return self.extent is not None

    def coverage(self, ctx: Context) -> list[dict]:
        return [{"label": self.name, "bbox": list(self.extent)}] if self.extent else []

    def _plan(self, bbox: BBox, res_m: float) -> list[tuple[BBox, int, int]]:
        """The exportImage requests (bbox, width px, height px) needed to cover bbox at res_m."""
        if self.extent:
            clipped = bbox.intersection(BBox(*self.extent))
            if not clipped:
                return []
            bbox = clipped
        xd, yd = meters_to_deg(res_m, bbox.center_lat)
        cw, ch = self.CHUNK * xd, self.CHUNK * yd
        jobs = []
        y = bbox.north
        while y > bbox.south + 1e-12:
            x = bbox.west
            while x < bbox.east - 1e-12:
                cb = BBox(x, max(bbox.south, y - ch), min(bbox.east, x + cw), y)
                w = max(1, round((cb.east - cb.west) / xd))
                h = max(1, round((cb.north - cb.south) / yd))
                jobs.append((cb, w, h))
                x += cw
            y -= ch
        return jobs

    def _key(self, cb: BBox, w: int, h: int) -> str:
        return hashlib.sha1(f"{self.url}|{cb.as_tuple()}|{w}|{h}|{self.band_ids}".encode()).hexdigest()[:20]

    def _cached_fraction(self, cache: Path, cb: BBox, w: int, h: int, depth: int = 0) -> float:
        """1.0 if this export is on disk; follows the quarter-split markers left by timeouts."""
        key = self._key(cb, w, h)
        if (cache / f"{key}.tif").exists():
            return 1.0
        if depth < 4 and (cache / f"{key}.split").exists():
            mx, my = (cb.west + cb.east) / 2, (cb.south + cb.north) / 2
            w1, h1 = w // 2, h // 2
            quads = [(BBox(cb.west, my, mx, cb.north), w1, h - h1), (BBox(mx, my, cb.east, cb.north), w - w1, h - h1),
                     (BBox(cb.west, cb.south, mx, my), w1, h1), (BBox(mx, cb.south, cb.east, my), w - w1, h1)]
            return sum(self._cached_fraction(cache, *q, depth + 1) for q in quads) / 4
        return 0.0

    def estimate(self, bbox: BBox, res_m: float, settings) -> dict:
        jobs = self._plan(bbox, res_m)
        if not jobs:
            return estimate_result(cached_pct=100, notes=["This service has no coverage here "
                                                          "(outside its area — e.g. US-only imagery)."],
                                   source_bytes=0, clipped_bytes=0)
        cache = settings.cache_dir / "arcgis" / self.id
        bytes_per_px = 4 if self.kind == "elevation" else (len(self.band_ids.split(",")) if self.band_ids else 3)
        total = todo = 0.0
        chunks_todo = 0.0
        for cb, w, h in jobs:
            size = w * h * bytes_per_px + 8_000  # the server sends uncompressed TIFF
            got = self._cached_fraction(cache, cb, w, h)
            total += size
            todo += size * (1 - got)
            chunks_todo += 1 - got
        rate, basis = speeds.get(settings, f"chunks:{self.id}")
        seconds = chunks_todo / rate
        cached_pct = 100.0 * (1 - todo / total) if total else 100.0
        n = len(jobs)
        notes = []
        if chunks_todo > 0:
            per = self.WORKERS / rate  # seconds each export takes on the server
            notes.append(f"{math.ceil(chunks_todo)} of {n} piece{'s' if n != 1 else ''} to request from the server, "
                         f"{self.WORKERS} at a time (~{per:.0f} s each) — {plain_duration(seconds)}.")
            if seconds >= 30 * 60:
                notes.append("This is a long download. A coarser detail level or a smaller area cuts it "
                             "down a lot (half the detail = a quarter of the pieces).")
        notes += cached_note(cached_pct, todo)
        if chunks_todo > 0:
            notes += speed_note(basis)
        return estimate_result(todo, cached_pct, n, seconds, notes, basis, source_bytes=total, clipped_bytes=total)

    def items(self, bbox: BBox, res_m: float, ctx: Context) -> list[Item]:
        jobs = self._plan(bbox, res_m)
        if not jobs:
            return []
        cache = ctx.settings.cache_dir / "arcgis" / self.id
        cache.mkdir(parents=True, exist_ok=True)
        pixel_type = "F32" if self.kind == "elevation" else "U8"
        fresh = [0]  # exports actually fetched from the server (for the learned speed)
        fresh_lock = threading.Lock()
        t_start = time.monotonic()

        def request(cb: BBox, w: int, h: int, dest: Path) -> bool:
            """One exportImage call with retries. False = the server kept timing out / 5xx."""
            params = {"bbox": ",".join(f"{v:.9f}" for v in cb.as_tuple()), "bboxSR": 4326, "imageSR": 4326,
                      "size": f"{w},{h}", "format": "tiff", "pixelType": pixel_type, "f": "image",
                      "interpolation": "RSP_BilinearInterpolation"}
            if self.band_ids:
                params["bandIds"] = self.band_ids
            with ctx.http(self.auth, timeout=180) as c:
                for attempt, delay in enumerate(self.RETRY_DELAYS_S):
                    ctx.check()
                    try:
                        r = c.get(f"{self.url}/exportImage", params=params)
                    except httpx.TransportError:  # includes read timeouts
                        r = None
                    if r is not None and r.status_code < 500:
                        r.raise_for_status()
                        if "tif" not in r.headers.get("content-type", "") and r.content[:4] not in (b"II*\x00", b"MM\x00*"):
                            raise RuntimeError(f"{self.name}: server returned {r.headers.get('content-type')}: {r.text[:200]}")
                        tmp = dest.with_suffix(".part")
                        tmp.write_bytes(r.content)
                        tmp.replace(dest)
                        with fresh_lock:
                            fresh[0] += (w * h) / (self.CHUNK * self.CHUNK)  # in full-chunk units
                        return True
                    if attempt < len(self.RETRY_DELAYS_S) - 1:
                        ctx.cancel_event.wait(delay)  # back off, but stay cancellable
            return False

        def fetch(cb: BBox, w: int, h: int) -> list[Item]:
            ctx.check()
            key = self._key(cb, w, h)
            dest, split_marker = cache / f"{key}.tif", cache / f"{key}.split"
            if dest.exists() or (not split_marker.exists() and request(cb, w, h, dest)):
                return [Item(path=str(dest), footprint=cb, native_res_m=res_m, gdal_env=self.auth.gdal_env(),
                             label=f"{self.id} export {cb.west:.4f},{cb.south:.4f}")]
            # Big exports time out (504) when the server is busy; quarter the request and retry.
            if min(w, h) < 2 * self.MIN_SPLIT:
                raise RuntimeError(f"{self.name}: server kept failing (5xx/timeout) for a {w}x{h} px export "
                                   f"at {cb.west:.5f},{cb.south:.5f}")
            split_marker.touch()  # a re-run goes straight to the cached quarters
            mx, my = (cb.west + cb.east) / 2, (cb.south + cb.north) / 2
            w1, h1 = w // 2, h // 2
            quads = [(BBox(cb.west, my, mx, cb.north), w1, h - h1), (BBox(mx, my, cb.east, cb.north), w - w1, h - h1),
                     (BBox(cb.west, cb.south, mx, my), w1, h1), (BBox(mx, cb.south, cb.east, my), w - w1, h1)]
            ctx.progress(f"{self.name}: server timed out on a {w}x{h} export — retrying as 4 smaller ones", None)
            return [item for q in quads for item in fetch(*q)]

        # Report in completion order: pool.map would stall the count behind one slow chunk.
        out: list[list[Item] | None] = [None] * len(jobs)
        ctx.progress(f"{self.name}: requesting {len(jobs)} export chunk(s) from the server", 0.0)
        with ThreadPoolExecutor(self.WORKERS) as pool:
            futures = {pool.submit(fetch, *job): n for n, job in enumerate(jobs)}
            try:
                for done, fut in enumerate(as_completed(futures), 1):
                    out[futures[fut]] = fut.result()
                    ctx.progress(f"{self.name}: fetched {done}/{len(jobs)} chunks", done / len(jobs))
            except Exception as e:
                if isinstance(e, Cancelled):
                    raise
                for f in futures:
                    f.cancel()
                cached = sum(1 for o in out if o is not None)
                raise RuntimeError(f"{e} — {cached}/{len(jobs)} chunks are cached; run the job again "
                                   f"to resume from them") from e
            finally:
                # Learn this server's real export rate (full-size chunks per second) for estimates.
                if fresh[0] >= 1:
                    speeds.record(ctx.settings, f"chunks:{self.id}", fresh[0], time.monotonic() - t_start)
        return [i for chunk in out if chunk for i in chunk]

    def chunk_count(self, bbox: BBox, res_m: float) -> int:
        """Number of exportImage requests items() will make (for the size estimate)."""
        return len(self._plan(bbox, res_m))


# --------------------------------------------------------------------------------------
# Tiled / OGC services through GDAL's WMS & WMTS drivers
# --------------------------------------------------------------------------------------
class TileService(Source):
    """XYZ ("{z}/{x}/{y}"), ArcGIS MapServer tiles, WMTS capabilities URLs or WMS GetMap.

    XYZ imagery (incl. ArcGIS tile endpoints) is downloaded by MapForge itself over a pooled
    connection and stitched locally (see sources/xyz.py) — much faster than GDAL, which opens a
    new connection per tile. WMS/WMTS and elevation tiles are still read through GDAL, lazily,
    at the zoom level / overview that matches the requested resolution, and cached on disk.
    """

    def __init__(self, id, name, url, group, service="xyz", layer="", max_zoom=19, image_format="image/jpeg",
                 kind="rgb", description="", license="", default_res_m=5.0, auth: Auth | None = None,
                 access="public", tile_matrix_set="", levels: list[dict] | None = None):
        self.id, self.name, self.url, self.group, self.service = id, name, url, group, service
        self.layer, self.max_zoom, self.image_format, self.kind = layer, int(max_zoom or 19), image_format, kind
        self.description, self.license, self.default_res_m = description, license, default_res_m
        self.auth, self.access, self.tile_matrix_set = auth or Auth(), access, tile_matrix_set
        self.min_res_m = round(web_mercator_res(self.max_zoom, 0), 2) if service == "xyz" else 0.1
        self.levels = levels

    def detail_levels(self) -> list[dict]:
        return self.levels or generic_detail_levels(self.kind, self.default_res_m, self.min_res_m)

    WMS_BLOCK = 1024  # px per WMS GetMap block (see _wms_xml)
    WMS_REQUESTS_PER_S = 0.5  # default for 1024 px GetMap blocks
    JPEG_BYTES_PER_PX = 0.35  # typical compressed photo tile

    def estimate(self, bbox: BBox, res_m: float, settings) -> dict:
        host = speeds.host_key("tiles", self.url)
        if self.service == "wms":
            xd, yd = meters_to_deg(res_m, bbox.center_lat)
            w = max(1, round((bbox.east - bbox.west) / xd))
            h = max(1, round((bbox.north - bbox.south) / yd))
            n = math.ceil(w / self.WMS_BLOCK) * math.ceil(h / self.WMS_BLOCK)
            size = w * h * (4 if self.kind == "elevation" else self.JPEG_BYTES_PER_PX)
            rate, basis = speeds.get(settings, host, self.WMS_REQUESTS_PER_S)
            what = f"{n:,} map image request{'s' if n != 1 else ''}"
        else:  # xyz / wmts (Web Mercator pyramid): tiles at the zoom the renderer will pick
            z = min(self.max_zoom, web_mercator_zoom_for(max(res_m, 0.05), bbox.center_lat))
            x0, y0 = _lonlat_to_tile(bbox.west, bbox.north, z)
            x1, y1 = _lonlat_to_tile(bbox.east, bbox.south, z)
            n = (abs(x1 - x0) + 1) * (abs(y1 - y0) + 1)
            tile_bytes, _ = speeds.get(settings, f"tilebytes:{self.id}")
            size = n * tile_bytes
            rate, basis = speeds.get(settings, host)
            what = f"{n:,} map tile{'s' if n != 1 else ''} (zoom level {z})"
            if self._fast_xyz() and n <= 50_000:  # tiles are plain files: count what's cached
                cache = self._tile_cache(settings)
                rng = xyz.tile_range(bbox, z)
                have = sum(xyz.is_cached(cache, z, x, y)
                           for y in range(rng[1], rng[3] + 1) for x in range(rng[0], rng[2] + 1))
                todo = n - have
                seconds = todo / rate
                notes = [f"{what}; {todo:,} still to fetch — {plain_duration(seconds)}."
                         if have else f"{what} to fetch — {plain_duration(seconds)}."]
                notes += speed_note(basis)
                return estimate_result(todo * tile_bytes, 100.0 * have / n if n else 0, n, seconds, notes, basis,
                                       source_bytes=size * 1.1, clipped_bytes=size * 1.1)
        seconds = n / rate
        notes = [f"{what} to fetch — {plain_duration(seconds)}.",
                 "Pieces fetched before are reused from the local cache, so repeat jobs are faster."]
        notes += speed_note(basis)
        # The GDAL tile cache can't be inspected cheaply, so the estimate assumes nothing is cached.
        return estimate_result(size, 0, n, seconds, notes, basis, source_bytes=size * 1.1, clipped_bytes=size * 1.1)

    def _cache(self, ctx: Context) -> Path:
        p = ctx.settings.cache_dir / "tiles" / self.id
        p.mkdir(parents=True, exist_ok=True)
        return p

    def _fast_xyz(self) -> bool:
        """XYZ imagery is downloaded by MapForge itself (see sources/xyz.py); others use GDAL."""
        return self.service == "xyz" and self.kind == "rgb"

    def _tile_cache(self, settings) -> Path:
        # Keyed by URL so a corrected custom-endpoint URL doesn't inherit "no tile here" markers.
        return settings.cache_dir / "tiles" / self.id / hashlib.sha1(self.url.encode()).hexdigest()[:10]

    def _zoom(self, bbox: BBox, res_m: float) -> int:
        return min(self.max_zoom, web_mercator_zoom_for(max(res_m, 0.05), bbox.center_lat))

    def _xyz_xml(self, ctx: Context) -> str:
        url = self.url.replace("{z}", "${z}").replace("{x}", "${x}").replace("{y}", "${y}")
        # RGB + alpha: GDAL marks missing (404/204) tiles transparent instead of black, so gaps
        # in a service never masquerade as valid pixels in the mosaic.
        bands = 1 if self.kind == "elevation" else 4
        return f"""<GDAL_WMS>
  <Service name="TMS"><ServerUrl>{escape(url)}</ServerUrl></Service>
  <DataWindow><UpperLeftX>{-MERC}</UpperLeftX><UpperLeftY>{MERC}</UpperLeftY>
    <LowerRightX>{MERC}</LowerRightX><LowerRightY>{-MERC}</LowerRightY>
    <TileLevel>{self.max_zoom}</TileLevel><TileCountX>1</TileCountX><TileCountY>1</TileCountY><YOrigin>top</YOrigin></DataWindow>
  <Projection>EPSG:3857</Projection><BlockSizeX>256</BlockSizeX><BlockSizeY>256</BlockSizeY><BandsCount>{bands}</BandsCount>
  <MaxConnections>8</MaxConnections><Timeout>60</Timeout><ZeroBlockHttpCodes>204,404</ZeroBlockHttpCodes>
  <UserAgent>{escape(ctx.settings.user_agent)}</UserAgent>
  <Cache><Path>{self._cache(ctx)}</Path></Cache>
</GDAL_WMS>"""

    def _wms_xml(self, bbox: BBox, res_m: float, ctx: Context) -> str:
        xd, yd = meters_to_deg(res_m, bbox.center_lat)
        w = max(1, round((bbox.east - bbox.west) / xd))
        h = max(1, round((bbox.north - bbox.south) / yd))
        return f"""<GDAL_WMS>
  <Service name="WMS"><Version>1.1.1</Version><ServerUrl>{escape(self.url)}</ServerUrl><SRS>EPSG:4326</SRS>
    <ImageFormat>{escape(self.image_format)}</ImageFormat><Layers>{escape(self.layer)}</Layers><Styles></Styles></Service>
  <DataWindow><UpperLeftX>{bbox.west}</UpperLeftX><UpperLeftY>{bbox.north}</UpperLeftY>
    <LowerRightX>{bbox.east}</LowerRightX><LowerRightY>{bbox.south}</LowerRightY>
    <SizeX>{w}</SizeX><SizeY>{h}</SizeY></DataWindow>
  <Projection>EPSG:4326</Projection><BlockSizeX>1024</BlockSizeX><BlockSizeY>1024</BlockSizeY><BandsCount>{1 if self.kind == "elevation" else 4}</BandsCount>
  <MaxConnections>4</MaxConnections><Timeout>120</Timeout><UserAgent>{escape(ctx.settings.user_agent)}</UserAgent>
  <Cache><Path>{self._cache(ctx)}</Path></Cache>
</GDAL_WMS>"""

    def items(self, bbox: BBox, res_m: float, ctx: Context) -> list[Item]:
        if self.service == "wmts":
            opts = [f"layer={self.layer}"] if self.layer else []
            if self.tile_matrix_set:
                opts.append(f"tilematrixset={self.tile_matrix_set}")
            path = "WMTS:" + ",".join([self.url, *opts])
            env = {**self.auth.gdal_env(), "GDAL_DEFAULT_WMS_CACHE_PATH": str(self._cache(ctx))}
            # No probe: opening a WMTS dataset already fetches GetCapabilities, so a bad URL or
            # credential fails immediately when the layer is opened for rendering.
            return [Item(path=path, footprint=bbox, label=self.name, gdal_env=env)]
        if self._fast_xyz():
            self._probe_xyz(bbox, res_m, ctx)
            z = self._zoom(bbox, res_m)
            rng = xyz.tile_range(bbox, z)
            n = (rng[2] - rng[0] + 1) * (rng[3] - rng[1] + 1)
            if n > xyz.MAX_TILES:
                raise TooManyTiles(f"{self.name}: {n:,} map tiles at this level of detail is too many for one "
                                   "job — splitting the area into smaller pieces", n / xyz.MAX_TILES)
            cache = self._tile_cache(ctx.settings)
            fetch_ctx = replace(ctx, progress=lambda m, f=None: ctx.progress(m, None if f is None else 0.9 * f))
            xyz.fetch_tiles(self.url, z, rng, cache, fetch_ctx, self.auth, self.name, self.id)
            build_ctx = replace(ctx, progress=lambda m, f=None: ctx.progress(m, None if f is None else 0.9 + 0.1 * f))
            mosaic = xyz.build_mosaic(cache, z, rng, build_ctx, self.name)
            return [Item(path=str(mosaic), footprint=bbox, label=self.name, service=True)]
        if self.service == "wms":
            xml = self._wms_xml(bbox, res_m, ctx)
        else:
            self._probe_xyz(bbox, res_m, ctx)
            xml = self._xyz_xml(ctx)
        p = self._cache(ctx) / f"{hashlib.sha1(xml.encode()).hexdigest()[:16]}.xml"
        p.write_text(xml)
        # native_res_m=None: the tile pyramid's finest zoom is not the data's true resolution.
        item = Item(path=str(p), footprint=bbox, label=self.name, gdal_env=self.auth.gdal_env())
        if self.service == "wms":
            self._probe_gdal(item, read=True)
        return [item]

    def probe_speed(self, bbox: BBox, res_m: float, ctx: Context) -> None:
        """Best-effort real tile fetch to seed a fresh this-run throughput sample (see
        mapforge.speeds) before the user has actually downloaded anything from this host.
        Silent on failure — this is a background nicety, not a correctness check."""
        if self.service != "xyz":
            return  # WMS/WMTS go through GDAL, which times its own real fetches during items()
        try:
            self._probe_xyz(bbox, res_m, ctx)
        except Exception:
            pass

    # -- fail-fast probes: a wrong URL, bad credentials or an empty area should produce a clear
    #    error in seconds, not a long render of blank tiles. ---------------------------------
    def _probe_xyz(self, bbox: BBox, res_m: float, ctx: Context) -> None:
        z = min(self.max_zoom, web_mercator_zoom_for(max(res_m, 0.05), bbox.center_lat))
        pts = {(bbox.west + (bbox.east - bbox.west) * fx, bbox.south + (bbox.north - bbox.south) * fy)
               for fx, fy in ((0.5, 0.5), (0.1, 0.1), (0.9, 0.9), (0.1, 0.9), (0.9, 0.1))}
        tiles = sorted({_lonlat_to_tile(lon, lat, z) for lon, lat in pts})
        statuses, dropped = [], []
        # A dropped connection on one probe tile says nothing about the service; try the other
        # tiles, and give the whole probe a second chance before calling the server unreachable.
        for attempt in range(2):
            if attempt:
                ctx.cancel_event.wait(3)
                ctx.check()
            statuses, dropped = [], []
            with ctx.http(self.auth, timeout=30) as c:
                for x, y in tiles:
                    url = self.url.replace("{z}", str(z)).replace("{x}", str(x)).replace("{y}", str(y))
                    t0 = time.monotonic()
                    try:
                        r = c.get(url)
                    except httpx.TransportError as e:
                        dropped.append(e)
                        continue
                    ctype = r.headers.get("content-type", "")
                    if r.status_code == 200 and ("image" in ctype or r.content[:4] in (b"\x89PNG", b"\xff\xd8\xff\xe0",
                                                                                       b"\xff\xd8\xff\xe1", b"\xff\xd8\xff\xdb")):
                        # One tile's size and latency; GDAL fetches TILE_CONNECTIONS in parallel.
                        speeds.observe(ctx.settings, f"tilebytes:{self.id}", len(r.content))
                        speeds.record(ctx.settings, speeds.host_key("tiles", self.url), TILE_CONNECTIONS,
                                      time.monotonic() - t0)
                        return
                    if r.status_code in (401, 403):
                        raise PermissionError(f"{self.name}: server rejected the request (HTTP {r.status_code}) — "
                                              "check credentials / certificate")
                    statuses.append(r.status_code if r.status_code != 200 else f"200 {ctype or 'non-image'}")
            if statuses:  # the server answered; retrying won't change a 404 into a tile
                break
        if dropped and not statuses:
            raise RuntimeError(f"{self.name}: cannot reach the tile server ({type(dropped[-1]).__name__}: "
                               f"{dropped[-1]}) — check the network, or try again in a few minutes") from dropped[-1]
        raise ValueError(f"{self.name}: no tiles for this area at zoom {z} (got {', '.join(map(str, statuses))}) — "
                         "check the URL template / layer, or the service has no coverage here")

    def _probe_gdal(self, item: Item, read: bool = False) -> None:
        import rasterio
        from rasterio.windows import Window

        try:
            with rasterio.Env(GDAL_HTTP_TIMEOUT=30, GDAL_HTTP_MAX_RETRY=1, **item.gdal_env), rasterio.open(item.path) as ds:
                if read:
                    ds.read(1, window=Window(0, 0, min(64, ds.width), min(64, ds.height)))
        except Exception as e:
            raise RuntimeError(f"{self.name}: service check failed — {str(e)[:300]}") from e


def _lonlat_to_tile(lon: float, lat: float, z: int) -> tuple[int, int]:
    n = 2**z
    lat = max(min(lat, 85.0511), -85.0511)
    x = int((lon + 180.0) / 360.0 * n)
    y = int((1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n)
    return min(max(x, 0), n - 1), min(max(y, 0), n - 1)


# --------------------------------------------------------------------------------------
# Built-in public catalogue
# --------------------------------------------------------------------------------------
CONUS = (-125.0, 24.0, -66.5, 49.5)


def _meta(src: Source, category: str, plain_name: str, explain: str) -> Source:
    src.category, src.plain_name, src.explain = category, plain_name, explain
    return src


def public_service_sources() -> list[Source]:
    img = "Imagery (public)"
    elev = "Elevation (public)"
    return [
        _meta(ArcGISImageServer(
            "usgs-naip", "USGS NAIP aerial (0.3–1 m, US)",
            "https://imagery.nationalmap.gov/arcgis/rest/services/USGSNAIPImagery/ImageServer",
            img, extent=CONUS, default_res_m=1.0, min_res_m=0.3,
            description="Leaf-on aerial orthophotography of the lower 48, 0.3–1 m. Best free high-res US imagery.",
            license="USDA NAIP, public domain.",
            levels=[_level("overview", "Regional overview", 10, "Towns, main roads, rivers and fields"),
                    _level("area", "Area detail", 3, "Streets, car parks and large buildings"),
                    _level("detailed", "Street-level", 1, "Individual buildings and trees; vehicles are a few pixels"),
                    _level("max", "Maximum detail", 0.6, "Finest NAIP available (0.6 m in most states) — slowest")]),
            "imagery", "Aerial photos — lower 48 US states (NAIP)",
            "Summer aerial photography of the continental US, sharp enough to see individual buildings. "
            "Slow to download at the highest detail levels."),
        _meta(TileService(
            "usgs-imagery", "USGS Imagery Only (global/US tiles)",
            "https://basemap.nationalmap.gov/arcgis/rest/services/USGSImageryOnly/MapServer/tile/{z}/{y}/{x}",
            img, max_zoom=16, default_res_m=2.0,
            description="The National Map imagery basemap tiles (NAIP + Blue Marble). Fast for large areas.",
            license="USGS The National Map, public domain.",
            levels=[_level("overview", "Regional overview", 20, "Towns, main roads and coastlines"),
                    _level("area", "Area detail", 5, "Streets and large buildings"),
                    _level("detailed", "Street-level", 2.5, "Individual buildings (US); coarser elsewhere")]),
            "imagery", "Photo basemap — US detailed, world coarse (USGS)",
            "Quick-loading photo map from USGS: NAIP-quality over the US, much coarser elsewhere. "
            "Good when speed matters more than the very finest detail."),
        _meta(TileService(
            "esri-world-imagery", "Esri World Imagery (global, sub-metre in many areas)",
            "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
            img, max_zoom=19, default_res_m=2.0,
            description="Esri's global satellite/aerial mosaic (Maxar, Airbus, USDA and other contributors). "
                       "Resolution varies a lot by location: sub-metre in many cities, coarser in remote areas — "
                       "the best free option MapForge ships for outside-the-US imagery.",
            license="© Esri and its data providers (Maxar, Airbus, USDA FSA, USGS, AeroGRID, IGN and the GIS "
                   "user community). Free to view/use under Esri's basemap terms, not public domain — check "
                   "Esri's terms before redistributing or for large-scale/government use.",
            levels=[_level("overview", "Regional overview", 20, "Towns, main roads and coastlines"),
                    _level("area", "Area detail", 5, "Streets and large buildings, most places"),
                    _level("detailed", "Street-level", 2, "Individual buildings, many populated areas"),
                    _level("max", "Maximum detail", 0.3, "Finest available where Esri has high-res coverage — "
                          "varies by location; some remote areas stay coarse")]),
            "imagery", "Satellite/aerial photos — worldwide, sharper in cities (Esri)",
            "A global photo mosaic that's much sharper than the Sentinel-2 layer in many populated areas "
            "(sometimes sub-metre), though quality varies a lot by location — some remote areas are still coarse. "
            "The best choice here for detailed imagery outside the US."),
        *[_meta(TileService(
            f"s2cloudless-{yr}", f"Sentinel-2 cloudless {yr} (10 m, global)",
            # EOX publishes the original 2016 mosaic as "s2cloudless", later years as "s2cloudless-YYYY".
            f"https://tiles.maps.eox.at/wmts/1.0.0/{'s2cloudless' if yr == '2016' else 's2cloudless-' + yr}_3857"
            "/default/g/{z}/{y}/{x}.jpg",
            img, max_zoom=15, default_res_m=10.0,
            description="Cloud-free global Sentinel-2 mosaic by EOX. Good worldwide 10 m base layer.",
            license=("CC BY 4.0 — 'Sentinel-2 cloudless by EOX IT Services GmbH (contains modified Copernicus "
                     "Sentinel data)'" if yr == "2016" else
                     "CC BY-NC-SA 4.0 — non-commercial only; buy a commercial licence from EOX otherwise."),
            levels=[_level("overview", "Wide-area overview", 40, "Coastlines, cities and big roads"),
                    _level("native", "Full detail (10 m)", 10, "Fields, towns and major roads; single buildings not visible")]),
            "imagery", f"Satellite photos — worldwide (Sentinel-2, {yr})",
            "A cloud-free satellite picture of the whole world. Works everywhere, but not sharp enough to "
            "see individual buildings." + (" Free for commercial use." if yr == "2016" else
                                           " Non-commercial licence."))
          for yr in ("2024", "2016")],
        CopernicusDEM(30),
        CopernicusDEM(90),
        _meta(ArcGISImageServer(
            "usgs-3dep", "USGS 3DEP elevation (1–10 m, US)",
            "https://elevation.nationalmap.gov/arcgis/rest/services/3DEPElevation/ImageServer",
            elev, kind="elevation", band_ids="", extent=(-180, -15, -60, 72), default_res_m=10.0, min_res_m=1.0,
            description="US bare-earth elevation (lidar-derived where available).",
            license="USGS, public domain.",
            levels=[_level("overview", "Coarse terrain (30 m)", 30, "About DTED Level 2 spacing; quick"),
                    _level("area", "Standard US terrain (10 m)", 10, "The usual US elevation detail"),
                    _level("detailed", "Detailed (3 m)", 3, "Small ridges and gullies"),
                    _level("max", "Maximum (1 m lidar)", 1, "Where lidar exists; slowest")]),
            "elevation", "Terrain elevation — US, high detail (3DEP)",
            "Bare-earth ground height for the US (buildings and trees removed), down to 1 m where lidar "
            "exists. Use for detailed terrain or high-resolution DTED."),
    ]
