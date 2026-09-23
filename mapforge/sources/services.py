"""Service-backed sources: cloud-optimised files, ArcGIS ImageServers, XYZ/WMTS/WMS.

These classes back both the built-in public sources and user-defined endpoints (which is
how PKI-protected services such as NGA GEGD are added — see sources/custom.py).
"""
from __future__ import annotations

import hashlib
import math
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from xml.sax.saxutils import escape

import httpx

from ..geo import BBox, meters_to_deg, web_mercator_res
from .base import Auth, Context, Item, Source

MERC = 20037508.342789244


# --------------------------------------------------------------------------------------
# Copernicus DEM (global elevation, COGs on AWS Open Data)
# --------------------------------------------------------------------------------------
class CopernicusDEM(Source):
    group = "Elevation (public)"
    kind = "elevation"
    access = "public"
    license = "Copernicus DEM © DLR e.V. / Airbus, provided under COPERNICUS by the EU and ESA; free incl. commercial use."

    def __init__(self, arcsec: int):
        self.arcsec = arcsec
        self.id = f"copernicus-dem-{arcsec}"
        self.name = f"Copernicus DEM GLO-{arcsec} ({'~30' if arcsec == 30 else '~90'} m)"
        self.description = "Global digital surface model. Exports cleanly to DTED."
        self.default_res_m = 30.0 if arcsec == 30 else 90.0
        self.min_res_m = self.default_res_m
        self.bucket = "copernicus-dem-30m" if arcsec == 30 else "copernicus-dem-90m"
        self.code = "10" if arcsec == 30 else "30"

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
            ctx.progress(f"{self.name}: tile {i}/{len(present)}", None)
            url = self.tile_url(lat, lon)
            local = ctx.download(url, cache / url.rsplit("/", 1)[1], label=f"DEM tile {i}/{len(present)}")
            out.append(Item(path=str(local), footprint=BBox(lon, lat, lon + 1, lat + 1),
                            label=f"{url.rsplit('/', 1)[1]}", native_res_m=self.default_res_m))
        return out


# --------------------------------------------------------------------------------------
# ArcGIS ImageServer exportImage (USGS NAIP, USGS 3DEP, many agency servers)
# --------------------------------------------------------------------------------------
class ArcGISImageServer(Source):
    access = "public"
    CHUNK = 2000  # px per request edge; servers cap at ~4000 and time out on big exports

    def __init__(self, id, name, url, group, kind="rgb", band_ids="0,1,2", extent=None, description="",
                 license="", default_res_m=1.0, min_res_m=0.3, auth: Auth | None = None, access="public"):
        self.id, self.name, self.url, self.group, self.kind = id, name, url.rstrip("/"), group, kind
        self.band_ids, self.extent, self.description, self.license = band_ids, extent, description, license
        self.default_res_m, self.min_res_m, self.auth, self.access = default_res_m, min_res_m, auth or Auth(), access

    def items(self, bbox: BBox, res_m: float, ctx: Context) -> list[Item]:
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
        cache = ctx.settings.cache_dir / "arcgis" / self.id
        cache.mkdir(parents=True, exist_ok=True)
        pixel_type = "F32" if self.kind == "elevation" else "U8"

        def fetch(job):
            cb, w, h = job
            ctx.check()
            key = hashlib.sha1(f"{self.url}|{cb.as_tuple()}|{w}|{h}|{self.band_ids}".encode()).hexdigest()[:20]
            dest = cache / f"{key}.tif"
            if not dest.exists():
                params = {"bbox": ",".join(f"{v:.9f}" for v in cb.as_tuple()), "bboxSR": 4326, "imageSR": 4326,
                          "size": f"{w},{h}", "format": "tiff", "pixelType": pixel_type, "f": "image",
                          "interpolation": "RSP_BilinearInterpolation"}
                if self.band_ids:
                    params["bandIds"] = self.band_ids
                with ctx.http(self.auth, timeout=180) as c:
                    for attempt in range(4):
                        ctx.check()
                        try:
                            r = c.get(f"{self.url}/exportImage", params=params)
                            if r.status_code < 500:
                                break
                        except httpx.TransportError:
                            if attempt == 3:
                                raise
                        time.sleep(3 * 2**attempt)
                    r.raise_for_status()
                    if "tif" not in r.headers.get("content-type", "") and not r.content[:4] in (b"II*\x00", b"MM\x00*"):
                        raise RuntimeError(f"{self.name}: server returned {r.headers.get('content-type')}: {r.text[:200]}")
                    tmp = dest.with_suffix(".part")
                    tmp.write_bytes(r.content)
                    tmp.replace(dest)
            return Item(path=str(dest), footprint=cb, native_res_m=res_m, gdal_env=self.auth.gdal_env(),
                        label=f"{self.id} export {cb.west:.4f},{cb.south:.4f}")

        out: list[Item] = []
        with ThreadPoolExecutor(4) as pool:
            for i, item in enumerate(pool.map(fetch, jobs), 1):
                ctx.progress(f"{self.name}: fetched {i}/{len(jobs)} chunks", None)
                out.append(item)
        return out


# --------------------------------------------------------------------------------------
# Tiled / OGC services through GDAL's WMS & WMTS drivers
# --------------------------------------------------------------------------------------
class TileService(Source):
    """XYZ ("{z}/{x}/{y}"), ArcGIS MapServer tiles, WMTS capabilities URLs or WMS GetMap.

    Tiles are fetched lazily by GDAL for only the area being processed, at the zoom level /
    overview that matches the requested resolution, and cached on disk for reuse.
    """

    def __init__(self, id, name, url, group, service="xyz", layer="", max_zoom=19, image_format="image/jpeg",
                 kind="rgb", description="", license="", default_res_m=5.0, auth: Auth | None = None,
                 access="public", tile_matrix_set=""):
        self.id, self.name, self.url, self.group, self.service = id, name, url, group, service
        self.layer, self.max_zoom, self.image_format, self.kind = layer, int(max_zoom or 19), image_format, kind
        self.description, self.license, self.default_res_m = description, license, default_res_m
        self.auth, self.access, self.tile_matrix_set = auth or Auth(), access, tile_matrix_set
        self.min_res_m = round(web_mercator_res(self.max_zoom, 0), 2) if service == "xyz" else 0.1

    def _cache(self, ctx: Context) -> Path:
        p = ctx.settings.cache_dir / "tiles" / self.id
        p.mkdir(parents=True, exist_ok=True)
        return p

    def _xyz_xml(self, ctx: Context) -> str:
        url = self.url.replace("{z}", "${z}").replace("{x}", "${x}").replace("{y}", "${y}")
        bands = 1 if self.kind == "elevation" else 3
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
  <Projection>EPSG:4326</Projection><BlockSizeX>1024</BlockSizeX><BlockSizeY>1024</BlockSizeY><BandsCount>3</BandsCount>
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
            return [Item(path=path, footprint=bbox, label=self.name, gdal_env=env)]
        if self.service == "wms":
            xml = self._wms_xml(bbox, res_m, ctx)
        else:
            xml = self._xyz_xml(ctx)
        p = self._cache(ctx) / f"{hashlib.sha1(xml.encode()).hexdigest()[:16]}.xml"
        p.write_text(xml)
        # native_res_m=None: the tile pyramid's finest zoom is not the data's true resolution.
        return [Item(path=str(p), footprint=bbox, label=self.name, gdal_env=self.auth.gdal_env())]


# --------------------------------------------------------------------------------------
# Built-in public catalogue
# --------------------------------------------------------------------------------------
CONUS = (-125.0, 24.0, -66.5, 49.5)


def public_service_sources() -> list[Source]:
    img = "Imagery (public)"
    elev = "Elevation (public)"
    return [
        ArcGISImageServer(
            "usgs-naip", "USGS NAIP aerial (0.3–1 m, US)",
            "https://imagery.nationalmap.gov/arcgis/rest/services/USGSNAIPImagery/ImageServer",
            img, extent=CONUS, default_res_m=1.0, min_res_m=0.3,
            description="Leaf-on aerial orthophotography of the lower 48, 0.3–1 m. Best free high-res US imagery.",
            license="USDA NAIP, public domain."),
        TileService(
            "usgs-imagery", "USGS Imagery Only (global/US tiles)",
            "https://basemap.nationalmap.gov/arcgis/rest/services/USGSImageryOnly/MapServer/tile/{z}/{y}/{x}",
            img, max_zoom=16, default_res_m=2.0,
            description="The National Map imagery basemap tiles (NAIP + Blue Marble). Fast for large areas.",
            license="USGS The National Map, public domain."),
        *[TileService(
            f"s2cloudless-{yr}", f"Sentinel-2 cloudless {yr} (10 m, global)",
            f"https://tiles.maps.eox.at/wmts/1.0.0/s2cloudless-{yr}_3857/default/g/{{z}}/{{y}}/{{x}}.jpg",
            img, max_zoom=15, default_res_m=10.0,
            description="Cloud-free global Sentinel-2 mosaic by EOX. Good worldwide 10 m base layer.",
            license=("CC BY 4.0 — 'Sentinel-2 cloudless by EOX IT Services GmbH (contains modified Copernicus "
                     "Sentinel data)'" if yr == "2016" else
                     "CC BY-NC-SA 4.0 — non-commercial only; buy a commercial licence from EOX otherwise."))
          for yr in ("2024", "2016")],
        CopernicusDEM(30),
        CopernicusDEM(90),
        ArcGISImageServer(
            "usgs-3dep", "USGS 3DEP elevation (1–10 m, US)",
            "https://elevation.nationalmap.gov/arcgis/rest/services/3DEPElevation/ImageServer",
            elev, kind="elevation", band_ids="", extent=(-180, -15, -60, 72), default_res_m=10.0, min_res_m=1.0,
            description="US bare-earth elevation (lidar-derived where available).",
            license="USGS, public domain."),
    ]
