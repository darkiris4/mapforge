"""Offline tests: synthetic rasters only, no network."""
from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import pytest
import rasterio
import rasterio.shutil
from rasterio.transform import from_bounds

from mapforge import sources
from mapforge.geo import BBox, grid_for, interior_depth, meters_to_deg, web_mercator_zoom_for
from mapforge.process import LayerOptions, OutputOptions, build_layer, dted_cell_grid, dted_lon_multiplier
from mapforge.settings import Settings
from mapforge.sources.base import Auth, Context
from mapforge.sources.custom import endpoint_to_source
from mapforge.sources.local import LocalProduct, scan


@pytest.fixture()
def settings(tmp_path):
    s = Settings(data_dir=tmp_path / "data", library_dirs=[tmp_path / "lib"])
    return s


def write_chart(path: Path, bounds, fill_index: int, collar_index: int, size=200, collar=20):
    """Paletted 'chart': interior colour + a collar band around the edge (like FAA sheets)."""
    a = np.full((size, size), collar_index, np.uint8)
    a[collar:-collar, collar:-collar] = fill_index
    with rasterio.open(path, "w", driver="GTiff", width=size, height=size, count=1, dtype="uint8",
                       crs="EPSG:4326", transform=from_bounds(*bounds, size, size)) as ds:
        ds.write(a, 1)
        ds.write_colormap(1, {0: (0, 0, 0, 255), 1: (255, 0, 0, 255), 2: (0, 0, 255, 255), 9: (255, 255, 255, 255)})


def write_dem(path: Path, bounds, size=120, base=100.0):
    y, x = np.mgrid[0:size, 0:size]
    z = (base + x + y).astype(np.float32)
    with rasterio.open(path, "w", driver="GTiff", width=size, height=size, count=1, dtype="float32",
                       crs="EPSG:4326", transform=from_bounds(*bounds, size, size), nodata=-9999) as ds:
        ds.write(z, 1)


class FileSource(sources.Source):
    def __init__(self, paths, kind="rgb", resampling="nearest"):
        self.paths, self.kind, self.resampling = paths, kind, resampling
        self.id, self.name, self.default_res_m = "test", "Test", 100.0

    def items(self, bbox, res_m, ctx):
        out = []
        for p in self.paths:
            with rasterio.open(p) as ds:
                b = BBox(*ds.bounds)
            out.append(sources.Item(path=str(p), footprint=b, label=p.name))
        return out


def test_bbox_and_grid():
    b = BBox.from_any([-77.2, 38.8, -76.9, 39.0])
    g = grid_for(b, 100)
    xd, yd = meters_to_deg(100, b.center_lat)
    assert abs(g.xres - xd) / xd < 0.01 and abs(g.yres - yd) / yd < 0.01
    with pytest.raises(ValueError):
        BBox.from_any([10, 0, 5, 1])
    assert web_mercator_zoom_for(10, 39) == 14


def test_interior_depth_prefers_center():
    fp = BBox(0, 0, 1, 1)
    d = interior_depth(np.array([0.5, 0.05, 1.5]), np.array([0.5, 0.5, 0.5]), fp)
    assert d[0] == pytest.approx(0.5) and d[1] == pytest.approx(0.05) and d[2] < 0


def test_dted_grids():
    t, nx, ny = dted_cell_grid(38, -77, 1)
    assert (nx, ny) == (1201, 1201)
    assert t.c == pytest.approx(-77 - 0.5 / 1200) and t.f == pytest.approx(39 + 0.5 / 1200)
    assert dted_lon_multiplier(55) == 2 and dted_lon_multiplier(-72) == 3
    _, nx, ny = dted_cell_grid(60, 10, 2)
    assert (nx, ny) == (1801, 3601)


def test_chart_mosaic_drops_collars(settings, tmp_path):
    # Two charts overlapping by their collars: A (red interior) west, B (blue interior) east.
    a, b = tmp_path / "a.tif", tmp_path / "b.tif"
    write_chart(a, (0.0, 0.0, 1.1, 1.0), fill_index=1, collar_index=9)
    write_chart(b, (0.9, 0.0, 2.0, 1.0), fill_index=2, collar_index=9)
    src = FileSource([a, b])
    out = tmp_path / "out"
    res = build_layer(src, BBox(0.2, 0.2, 1.8, 0.8), LayerOptions(res_m=1000), OutputOptions(), out,
                      Context(settings), "mosaic")
    assert res["status"] == "ok" and res["coverage_pct"] == 100.0
    with rasterio.open(out / "mosaic.tif") as ds:
        assert ds.crs.to_epsg() == 4326 and ds.count == 3
        rgb = ds.read()
    white = (rgb[0] == 255) & (rgb[1] == 255) & (rgb[2] == 255)
    assert not white.any(), "collar pixels leaked into the mosaic"
    assert (rgb[0, :, 0] == 255).all() and (rgb[2, :, -1] == 255).all()  # red west, blue east


def test_elevation_geotiff_and_dted(settings, tmp_path):
    dem = tmp_path / "dem.tif"
    write_dem(dem, (-78.0, 38.0, -76.0, 39.0), size=240)
    src = FileSource([dem], kind="elevation", resampling="bilinear")
    out = tmp_path / "out"
    res = build_layer(src, BBox(-77.6, 38.2, -76.4, 38.8), LayerOptions(res_m=2000),
                      OutputOptions(dted_level=0), out, Context(settings), "dem")
    assert "dted/w078/n38.dt0" in res["files"] and "dted/w077/n38.dt0" in res["files"]
    with rasterio.open(out / "dted/w077/n38.dt0") as ds:
        assert ds.driver == "DTED" and (ds.width, ds.height) == (121, 121)
        z = ds.read(1)
        assert z.min() >= 100 and z.max() <= 100 + 480
    with rasterio.open(out / "dem.tif") as ds:
        assert ds.dtypes[0] == "float32" and ds.nodata == -32767


def test_local_library_scan(settings, tmp_path):
    lib = settings.library_dirs[0]
    (lib / "dted" / "w077").mkdir(parents=True)
    (lib / "imagery").mkdir()
    t, nx, ny = dted_cell_grid(38, -77, 0)
    with rasterio.open(tmp_path / "d.tif", "w", driver="GTiff", width=nx, height=ny, count=1, dtype="int16",
                       crs="EPSG:4326", transform=t) as ds:
        ds.write(np.full((ny, nx), 50, np.int16), 1)
    rasterio.shutil.copy(tmp_path / "d.tif", lib / "dted" / "w077" / "n38.dt0", driver="DTED")
    write_chart(lib / "imagery" / "chart.tif", (-77.0, 38.0, -76.0, 39.0), 1, 9)
    idx = scan(settings)
    by_name = {p["name"]: p for p in idx["products"]}
    assert set(by_name) == {"DTED Level 0", "imagery (imagery)"}
    assert by_name["DTED Level 0"]["kind"] == "elevation" and by_name["imagery (imagery)"]["kind"] == "rgb"
    items = LocalProduct(by_name["imagery (imagery)"]).items(BBox(-76.8, 38.2, -76.2, 38.8), 100, Context(settings))
    assert items and items[0].footprint.west == pytest.approx(-77.0)


def test_endpoint_auth_mapping():
    ep = {"id": "gegd", "name": "GEGD", "type": "wmts", "url": "https://example.invalid/wmts?SERVICE=WMTS",
          "layer": "x", "auth": {"type": "pki", "cert": "/c.pem", "key": "/k.pem", "key_password": "pw"}}
    src = endpoint_to_source(ep)
    assert src.access == "pki" and src.group.startswith("NGA")
    env = src.auth.gdal_env()
    assert env["GDAL_HTTP_SSLCERT"] == "/c.pem" and env["GDAL_HTTP_SSLKEY"] == "/k.pem"
    assert Auth.from_dict(ep["auth"]).public()["key_password"] == "********"
    items = src.items(BBox(0, 0, 1, 1), 10, Context(Settings(data_dir=Path("/tmp/mapforge-test"))))
    assert items[0].path.startswith("WMTS:https://example.invalid") and "layer=x" in items[0].path


def test_api_roundtrip(settings, tmp_path):
    from fastapi.testclient import TestClient

    from mapforge.app import create_app

    write_chart(settings.library_dirs[0] / "chart.tif", (-77.0, 38.0, -76.0, 39.0), 1, 9)
    scan(settings)
    sources.refresh(settings)
    client = TestClient(create_app(settings))
    srcs = client.get("/api/sources").json()
    local = next(s for s in srcs if s["access"] == "local")
    spec = {"name": "t", "bbox": [-76.8, 38.2, -76.2, 38.8], "layers": [{"source": local["id"], "res_m": 500}],
            "outputs": {"geotiff": True}}
    assert client.post("/api/estimate", json=spec).json()["layers"][0]["width"] > 0
    jid = client.post("/api/jobs", json=spec).json()["id"]
    for _ in range(100):
        j = client.get(f"/api/jobs/{jid}").json()
        if j["status"] not in ("queued", "running"):
            break
        time.sleep(0.1)
    assert j["status"] == "done", j
    r = client.get(f"/api/jobs/{jid}/download")
    assert r.status_code == 200 and r.content[:2] == b"PK"
    assert client.post("/api/endpoints", json={"name": "x", "type": "bogus", "url": "u"}).status_code == 400
