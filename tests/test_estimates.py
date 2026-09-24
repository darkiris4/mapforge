"""Download size/time estimates and learned speeds (offline)."""
from __future__ import annotations

import http.server
import json
import threading

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_bounds

from mapforge import speeds, sources
from mapforge.geo import BBox
from mapforge.jobs import JobManager
from mapforge.settings import Settings
from mapforge.sources import faa as faa_mod
from mapforge.sources.base import Context
from mapforge.sources.faa import FaaSource, faa_sources
from mapforge.sources.local import LocalProduct, scan
from mapforge.sources.services import ArcGISImageServer, CopernicusDEM, TileService, public_service_sources
from mapforge.sources.ziputil import ZipMember

CATEGORIES = {"vfr", "ifr", "imagery", "elevation", "local", "custom"}
LEVEL_IDS = {"overview", "regional", "area", "detailed", "max", "native"}
ESTIMATE_KEYS = {"download_mb", "cached_pct", "requests", "download_seconds", "notes", "speed_basis"}


@pytest.fixture()
def settings(tmp_path):
    speeds.reset_cache()
    s = Settings(data_dir=tmp_path / "data", library_dirs=[tmp_path / "lib"])
    yield s
    speeds.reset_cache()


# -- contract: plain metadata on every built-in source ---------------------------------
@pytest.mark.parametrize("src", [*faa_sources(), *public_service_sources()], ids=lambda s: s.id)
def test_every_builtin_source_has_plain_metadata(src):
    info = src.info()
    assert info["category"] in CATEGORIES
    assert info["plain_name"] and info["explain"] and info["explain"].endswith(".")
    levels = info["detail_levels"]
    assert levels, "at least one detail level"
    assert all(set(lv) == {"id", "label", "res_m", "hint"} and lv["id"] in LEVEL_IDS for lv in levels)
    res = [lv["res_m"] for lv in levels]
    assert res == sorted(res, reverse=True), "ordered coarse -> fine"
    assert all(r >= src.min_res_m * 0.999 for r in res)


def test_categories_of_builtins():
    by_id = {s.id: s.info()["category"] for s in [*faa_sources(), *public_service_sources()]}
    assert by_id["faa-sectional"] == "vfr" and by_id["faa-ifr-low"] == "ifr"
    assert by_id["usgs-naip"] == by_id["s2cloudless-2024"] == by_id["usgs-imagery"] == "imagery"
    assert by_id["copernicus-dem-30"] == by_id["usgs-3dep"] == "elevation"


# -- speeds -------------------------------------------------------------------------
def test_speeds_ewma_persist_and_basis(settings):
    assert speeds.get(settings, "bytes:example.org") == (speeds.DEFAULTS["bytes"], "default")
    speeds.record(settings, "bytes:example.org", 10e6, 2.0)  # 5 MB/s
    assert speeds.get(settings, "bytes:example.org") == (pytest.approx(5e6), "measured")
    speeds.record(settings, "bytes:example.org", 10e6, 10.0)  # 1 MB/s -> EWMA 0.7*5 + 0.3*1
    assert speeds.get(settings, "bytes:example.org")[0] == pytest.approx(3.8e6)
    speeds.reset_cache()  # survives a restart
    saved = json.loads((settings.config_dir / "speeds.json").read_text())
    assert saved["bytes:example.org"]["n"] == 2
    assert speeds.get(settings, "bytes:example.org")[0] == pytest.approx(3.8e6)
    speeds.record(settings, "bytes:x", 0, 1.0)  # junk samples are ignored
    assert speeds.get(settings, "bytes:x")[1] == "default"


class _Handler(http.server.BaseHTTPRequestHandler):
    body = b"x" * (3 << 20)

    def do_GET(self):  # noqa: N802
        import time

        self.send_response(200)
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        for i in range(0, len(self.body), 1 << 20):  # a little slower than loopback, like a real server
            self.wfile.write(self.body[i:i + (1 << 20)])
            time.sleep(0.05)

    def log_message(self, *a):
        pass


def test_download_records_host_speed(settings, tmp_path):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{srv.server_port}/f.bin"
        Context(settings).download(url, tmp_path / "f.bin")
    finally:
        srv.shutdown()
    rate, basis = speeds.get(settings, speeds.host_key("bytes", url))
    assert basis == "measured" and rate > 1e5


# -- ArcGIS ImageServer ----------------------------------------------------------------
def _naip():
    return ArcGISImageServer("naip-t", "T", "https://example.invalid/ImageServer", "g", default_res_m=1)


def test_arcgis_estimate_uncached_then_cached(settings, monkeypatch):
    monkeypatch.setattr(ArcGISImageServer, "CHUNK", 100)
    src = _naip()
    bbox = BBox(0.0, 0.0, 0.0035, 0.0017)  # ~390 x 190 px at 1 m -> 4 x 2 chunks of <=100 px
    jobs = src._plan(bbox, 1)
    assert len(jobs) == src.chunk_count(bbox, 1) == 8
    e = src.estimate(bbox, 1, settings)
    assert set(e) >= ESTIMATE_KEYS and e["cached_pct"] == 0 and e["requests"] == 8
    assert e["speed_basis"] == "default"
    assert e["download_seconds"] == pytest.approx(8 / speeds.DEFAULTS["chunks"], abs=1)
    assert e["download_mb"] == pytest.approx(sum(w * h * 3 for _, w, h in jobs) / 1e6, abs=0.1)
    # Half the pieces on disk (one of them only as timed-out quarters) -> half the download.
    cache = settings.cache_dir / "arcgis" / src.id
    cache.mkdir(parents=True)
    for cb, w, h in jobs[:3]:
        (cache / f"{src._key(cb, w, h)}.tif").write_bytes(b"x")
    cb, w, h = jobs[3]
    (cache / f"{src._key(cb, w, h)}.split").touch()
    mx, my = (cb.west + cb.east) / 2, (cb.south + cb.north) / 2
    w1, h1 = w // 2, h // 2
    for q in [(BBox(cb.west, my, mx, cb.north), w1, h - h1), (BBox(mx, my, cb.east, cb.north), w - w1, h - h1),
              (BBox(cb.west, cb.south, mx, my), w1, h1), (BBox(mx, cb.south, cb.east, my), w - w1, h1)]:
        (cache / f"{src._key(*q)}.tif").write_bytes(b"x")
    e2 = src.estimate(bbox, 1, settings)
    assert 45 <= e2["cached_pct"] <= 55
    assert e2["download_seconds"] == pytest.approx(e["download_seconds"] / 2, abs=1)
    assert any("already downloaded" in n for n in e2["notes"])
    # A measured, faster server shortens the estimate and says so.
    speeds.record(settings, f"chunks:{src.id}", 4, 8.0)  # 0.5 chunks/s
    e3 = src.estimate(bbox, 1, settings)
    assert e3["speed_basis"] == "measured" and e3["download_seconds"] == 8


def test_arcgis_outside_extent(settings):
    src = public_service_sources()[0]  # NAIP, CONUS only
    e = src.estimate(BBox(10.0, 50.0, 10.1, 50.1), 1, settings)
    assert e["download_mb"] == 0 and e["cached_pct"] == 100 and "coverage" in e["notes"][0]


# -- FAA -----------------------------------------------------------------------------
def test_faa_estimate_counts_only_missing_charts(settings, monkeypatch):
    src = next(s for s in faa_sources() if s.id == "faa-sectional")
    entries = [
        {"url": "https://x/09-03-2026/sectional-files/A.zip", "member": "A SEC.tif", "bbox": [-78, 36, -72, 40],
         "edition": "09-03-2026", "res_m": 42, "label": "A SEC"},
        {"url": "https://x/09-03-2026/sectional-files/B.zip", "member": "B SEC.tif", "bbox": [-78, 40, -72, 44],
         "edition": "09-03-2026", "res_m": 42, "label": "B SEC"},
        {"url": "https://x/09-03-2026/sectional-files/C.zip", "member": "C SEC.tif", "bbox": [-120, 30, -110, 35],
         "edition": "09-03-2026", "res_m": 42, "label": "C SEC"},  # does not intersect
    ]
    monkeypatch.setattr(FaaSource, "_index", lambda self, ctx: entries)
    probed = []

    def fake_info(client, url):
        probed.append(url)
        name = url.rsplit("/", 1)[1][0]
        return 50_000_000, [ZipMember(f"{name} SEC.tif", 80_000_000, 49_000_000, 8, 0)]

    monkeypatch.setattr(faa_mod, "remote_zip_info", fake_info)
    bbox = BBox(-76.0, 39.0, -74.0, 41.0)  # half in A, half in B
    e = src.estimate(bbox, 42, settings)
    assert sorted(probed) == [entries[0]["url"], entries[1]["url"]]
    assert e["requests"] == 2 and e["download_mb"] == 100.0 and e["cached_pct"] == 0
    assert e["source_mb"] == 160.0  # two whole charts
    assert e["clipped_mb"] == pytest.approx(2 * 80 * (2 * 1) / (6 * 4), abs=0.2)
    # Chart A already extracted -> only B to download; sizes come from the cache (no new probes).
    folder = settings.cache_dir / "faa" / "09-03-2026"
    folder.mkdir(parents=True)
    (folder / "A SEC.tif").write_bytes(b"x")
    probed.clear()
    e2 = src.estimate(bbox, 42, settings)
    assert probed == [] and e2["download_mb"] == 50.0 and e2["cached_pct"] == 50 and e2["requests"] == 1


# -- Copernicus ------------------------------------------------------------------------
class _FakeHead:
    def __init__(self, sizes):
        self.sizes, self.calls = sizes, []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def head(self, url):
        self.calls.append(url)
        size = next((v for k, v in self.sizes.items() if k in url), None)

        class R:
            status_code = 200 if size else 404
            headers = {"content-length": str(size or 0)}
        return R()


def test_copernicus_estimate_ocean_partial_and_cached(settings, monkeypatch):
    src = CopernicusDEM(30)
    fake = _FakeHead({"N38_00_W078": 20_000_000, "N38_00_W077": 10_000_000})  # W076 = ocean (404)
    monkeypatch.setattr(Context, "http", lambda self, auth=None, timeout=60: fake)
    bbox = BBox(-77.9, 38.1, -75.5, 38.9)  # W078 80% covered (full download), W077 full, W076 ocean
    e = src.estimate(bbox, 30, settings)
    assert e["requests"] == 2 and e["source_mb"] == 30.0
    assert e["download_mb"] == 30.0 and len(fake.calls) == 3
    # Tiny overlap with a tile -> only a windowed read is counted.
    small = BBox(-77.05, 38.1, -76.95, 38.2)  # 0.05 x 0.1 of W078 and W077 each (0.5% of a tile)
    e2 = src.estimate(small, 30, settings)
    assert len(fake.calls) == 3, "tile sizes are cached"
    assert e2["download_mb"] < 1 and any("needed part" in n for n in e2["notes"])
    assert e2["cached_pct"] == 0, "a windowed read of an uncached tile is not 'already downloaded'"
    # Cached tile -> no download.
    cache = settings.cache_dir / "dem" / src.id
    (cache / src.tile_url(38, -78).rsplit("/", 1)[1]).write_bytes(b"x")
    (cache / src.tile_url(38, -77).rsplit("/", 1)[1]).write_bytes(b"x")
    e3 = src.estimate(bbox, 30, settings)
    assert e3["download_mb"] == 0 and e3["cached_pct"] == 100
    ocean = src.estimate(BBox(-75.9, 38.1, -75.1, 38.9), 30, settings)
    assert ocean["download_mb"] == 0 and "ocean" in ocean["notes"][0]


# -- tile services ---------------------------------------------------------------------
def test_tile_estimate_uses_zoom_and_learned_tile_size(settings):
    src = TileService("t", "T", "https://tiles.example/{z}/{x}/{y}.jpg", "g", max_zoom=15, default_res_m=10)
    bbox = BBox(-77.2, 38.8, -76.9, 39.0)
    e = src.estimate(bbox, 10, settings)
    assert "zoom level 14" in e["notes"][0] and e["requests"] > 10
    speeds.observe(settings, "tilebytes:t", 50_000)
    e2 = src.estimate(bbox, 10, settings)
    assert e2["download_mb"] == pytest.approx(e["requests"] * 0.05, abs=0.1)
    coarse = src.estimate(bbox, 40, settings)
    assert coarse["requests"] < e["requests"] / 8


# -- local library + full estimate response -------------------------------------------
def _write(path, bounds, size=100):
    with rasterio.open(path, "w", driver="GTiff", width=size, height=size, count=3, dtype="uint8",
                       crs="EPSG:4326", transform=from_bounds(*bounds, size, size)) as ds:
        ds.write(np.full((3, size, size), 7, np.uint8))


def test_local_estimate_no_download(settings):
    lib = settings.library_dirs[0]
    (lib / "imgs").mkdir(parents=True)
    _write(lib / "imgs" / "a.tif", (-77, 38, -76, 39))
    src = LocalProduct(scan(settings)["products"][0])
    assert src.info()["category"] == "local" and src.info()["detail_levels"]
    e = src.estimate(BBox(-76.75, 38.25, -76.25, 38.75), 1000, settings)
    size = (lib / "imgs" / "a.tif").stat().st_size / 1e6
    assert e["download_mb"] == 0 and e["cached_pct"] == 100 and e["download_seconds"] == 0
    assert e["source_mb"] == pytest.approx(size, abs=0.1)
    assert e["clipped_mb"] == pytest.approx(size / 4, abs=0.1)


def test_estimate_response_contract_and_modes(settings):
    lib = settings.library_dirs[0]
    (lib / "imgs").mkdir(parents=True)
    _write(lib / "imgs" / "a.tif", (-77, 38, -76, 39), size=400)
    scan(settings)
    sources.refresh(settings)
    jm = JobManager(settings)
    local_id = next(s.id for s in sources.registry(settings).values() if s.access == "local")
    spec = {"bbox": [-76.8, 38.2, -76.2, 38.8], "layers": [{"source": local_id, "res_m": 500}],
            "outputs": {"geotiff": True}}
    r = jm.estimate(spec)
    assert {"area_km", "layers", "total_download_mb", "total_download_seconds", "total_package_mb",
            "total_mb"} <= set(r)
    row = r["layers"][0]
    assert {"download_mb", "cached_pct", "download_seconds", "package_mb", "speed_basis", "notes",
            "est_mb", "width", "too_big"} <= set(row)
    assert r["total_mb"] == r["total_package_mb"] == row["package_mb"] == row["est_mb"]
    assert r["total_download_mb"] == 0 and r["total_download_seconds"] == 0
    cog = jm.estimate({**spec, "outputs": {"geotiff": True, "cog": True}})
    assert cog["layers"][0]["package_mb"] == pytest.approx(2 * row["est_mb"], abs=0.2)
    orig = jm.estimate({**spec, "mode": "original"})["layers"][0]
    clip = jm.estimate({**spec, "mode": "clipped"})["layers"][0]
    size = (lib / "imgs" / "a.tif").stat().st_size / 1e6
    assert orig["package_mb"] == pytest.approx(size, abs=0.1)
    assert clip["package_mb"] == pytest.approx(size * 0.36, abs=0.1)


def test_estimate_dted_adds_cells(settings):
    sources.refresh(settings)
    jm = JobManager(settings)
    spec = {"bbox": [-106.6, 31.7, -106.2, 31.9], "layers": [{"source": "usgs-3dep", "res_m": 30}],
            "outputs": {"geotiff": False, "dted_level": 1}}
    row = jm.estimate(spec)["layers"][0]
    assert row["package_mb"] == pytest.approx(2.9)  # one DTED1 cell, no GeoTIFF


def test_estimate_never_fails_on_source_error(settings, monkeypatch):
    sources.refresh(settings)
    jm = JobManager(settings)

    def boom(self, *a):
        raise RuntimeError("network down")

    monkeypatch.setattr(ArcGISImageServer, "estimate", boom)
    row = jm.estimate({"bbox": [-106.6, 31.7, -106.5, 31.8], "layers": [{"source": "usgs-naip"}],
                       "outputs": {"geotiff": True}})["layers"][0]
    assert "network down" in row["notes"][0] and row["download_mb"] == 0
