"""Offline tests for service sources, PKI/mutual-TLS auth and the local library."""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_bounds

from mapforge.geo import BBox
from mapforge.process import LayerOptions, OutputOptions, build_layer
from mapforge.settings import Settings
from mapforge.sources.base import Auth, Context
from mapforge.sources.custom import endpoint_to_source
from mapforge.sources.local import LocalProduct, _ecrg_product, _rpf_product, scan
from mapforge.sources.services import ArcGISImageServer, TileService, _lonlat_to_tile

sys.path.insert(0, str(Path(__file__).parent))
from pki_server import COLOR, KEY_PASSWORD, PkiServer, make_pki  # noqa: E402

AREA = BBox(-77.05, 38.88, -77.03, 38.90)


@pytest.fixture()
def settings(tmp_path):
    return Settings(data_dir=tmp_path / "data", library_dirs=[tmp_path / "lib"])


@pytest.fixture(scope="module")
def pki(tmp_path_factory):
    if not shutil.which("openssl"):
        pytest.skip("openssl not available")
    files = make_pki(tmp_path_factory.mktemp("pki"))
    with PkiServer(files) as srv:
        yield files, srv


def _auth(files, cert=True) -> Auth:
    if not cert:
        return Auth(type="none", ca_bundle=str(files.ca))
    return Auth(type="pki", cert=str(files.client_cert), key=str(files.client_key), key_password=KEY_PASSWORD,
                ca_bundle=str(files.ca))


def _assert_colour(path: Path):
    with rasterio.open(path) as ds:
        rgb = ds.read()
        mask = ds.dataset_mask()
    assert mask.all()
    for band, c in zip(rgb, COLOR):
        assert abs(int(np.median(band)) - c) <= 2


# ---------------------------------------------------------------------------------- PKI
def test_pki_xyz_tiles_through_gdal(pki, settings, tmp_path):
    files, srv = pki
    src = TileService("pki-xyz", "PKI XYZ", srv.url + "/tiles/{z}/{x}/{y}.png", "NGA", max_zoom=12,
                      auth=_auth(files), access="pki")
    r = build_layer(src, AREA, LayerOptions(res_m=50), OutputOptions(overviews=False), tmp_path / "o", Context(settings), "x")
    assert r["status"] == "ok" and r["coverage_pct"] == 100.0
    _assert_colour(tmp_path / "o" / "x.tif")


def test_pki_wms_through_gdal(pki, settings, tmp_path):
    files, srv = pki
    src = endpoint_to_source({"id": "w", "name": "PKI WMS", "type": "wms", "url": srv.url + "/wms", "layer": "a",
                              "format": "image/png", "auth": vars(_auth(files))})
    r = build_layer(src, AREA, LayerOptions(res_m=20), OutputOptions(overviews=False), tmp_path / "o", Context(settings), "w")
    assert r["status"] == "ok" and r["coverage_pct"] == 100.0
    _assert_colour(tmp_path / "o" / "w.tif")


def test_pki_arcgis_image_through_httpx(pki, settings, tmp_path):
    files, srv = pki
    src = ArcGISImageServer("pki-ags", "PKI ImageServer", srv.url + "/arcgis/ImageServer", "NGA", auth=_auth(files))
    r = build_layer(src, AREA, LayerOptions(res_m=5), OutputOptions(overviews=False), tmp_path / "o", Context(settings), "a")
    assert r["status"] == "ok" and r["coverage_pct"] == 100.0
    _assert_colour(tmp_path / "o" / "a.tif")


@pytest.mark.parametrize("kind", ["xyz", "wms", "arcgis"])
def test_pki_rejected_without_client_cert(pki, settings, tmp_path, kind):
    files, srv = pki
    auth = _auth(files, cert=False)
    if kind == "xyz":
        src = TileService("n1", "no cert", srv.url + "/tiles/{z}/{x}/{y}.png", "g", max_zoom=12, auth=auth)
    elif kind == "wms":
        src = TileService("n2", "no cert", srv.url + "/wms", "g", service="wms", layer="a", auth=auth)
    else:
        src = ArcGISImageServer("n3", "no cert", srv.url + "/arcgis/ImageServer", "g", auth=auth)
    with pytest.raises(Exception):
        build_layer(src, AREA, LayerOptions(res_m=20), OutputOptions(), tmp_path / "o", Context(settings), "n")


def test_auth_env_overrides_wheel_ca_bundle():
    env = Auth(type="pki", cert="c", key="k", ca_bundle="/dod.pem").gdal_env()
    # rasterio wheels pre-set GDAL_CURL_CA_BUNDLE, which beats GDAL_HTTP_CAINFO — both must be set.
    assert env["GDAL_CURL_CA_BUNDLE"] == env["GDAL_HTTP_CAINFO"] == "/dod.pem"


# ----------------------------------------------------------------------- tile service probes
def test_lonlat_to_tile():
    assert _lonlat_to_tile(0, 0, 1) == (1, 1)
    assert _lonlat_to_tile(-180, 85, 3) == (0, 0)
    assert _lonlat_to_tile(179.99, -85, 3) == (7, 7)


def test_tile_probe_fails_fast_on_missing_tiles(pki, settings):
    files, srv = pki
    src = TileService("bad", "Bad", srv.url + "/nothing/{z}/{x}/{y}.png", "g", max_zoom=12, auth=_auth(files))
    with pytest.raises(ValueError, match="no tiles"):
        src.items(AREA, 50, Context(settings))


def test_tile_probe_unreachable(settings):
    src = TileService("u", "U", "https://127.0.0.1:9/{z}/{x}/{y}.png", "g")
    with pytest.raises(RuntimeError, match="cannot reach"):
        src.items(AREA, 50, Context(settings))


# ------------------------------------------------------------------------------ library
def _write(path, bands, dtype="uint8", bounds=(-77.0, 38.0, -76.0, 39.0), size=64, driver="GTiff", value=None, **kw):
    arr = np.full((bands, size, size), 0, dtype)
    for i in range(bands):
        arr[i] = value[i] if value is not None else (i + 1) * 40
    with rasterio.open(path, "w", driver=driver, width=size, height=size, count=bands, dtype=dtype,
                       crs="EPSG:4326", transform=from_bounds(*bounds, size, size), **kw) as ds:
        ds.write(arr)


def test_scan_survives_junk_and_indexes_formats(settings):
    lib = settings.library_dirs[0]
    (lib / "mixed").mkdir(parents=True)
    (lib / "mixed" / "empty.tif").write_bytes(b"")
    (lib / "mixed" / "garbage.ntf").write_bytes(b"NITF02.10" + b"\0" * 50)
    (lib / "mixed" / "A.TOC.bak").write_text("x")
    (lib / "bogus_rpf").mkdir()
    (lib / "bogus_rpf" / "A.TOC").write_bytes(b"not a toc")
    (lib / ".hidden").mkdir()
    _write(lib / ".hidden" / "x.tif", 3)
    with rasterio.open(lib / "mixed" / "nogeo.tif", "w", driver="GTiff", width=8, height=8, count=1, dtype="uint8") as d:
        d.write(np.zeros((1, 8, 8), np.uint8))
    _write(lib / "mixed" / "gray.tif", 1)
    _write(lib / "mixed" / "rgb.tif", 3, bounds=(-76.5, 38.0, -75.5, 39.0))
    _write(lib / "mixed" / "rgba.tif", 4, bounds=(-77.5, 38.0, -76.5, 39.0), photometric="RGB", alpha="YES",
           value=[10, 20, 30, 255])
    _write(lib / "mixed" / "img16.tif", 3, dtype="uint16", bounds=(-77.2, 38.2, -76.8, 38.8), value=[4000, 2000, 1000])
    _write(lib / "mixed" / "chip.ntf", 3, driver="NITF", ICORDS="G")
    _write(lib / "mixed" / "chip.jp2", 3, driver="JP2OpenJPEG", bounds=(-76.2, 38.0, -75.8, 38.4))
    _write(lib / "mixed" / "dem.tif", 1, dtype="float32")

    idx = scan(settings)
    by = {p["name"]: p for p in idx["products"]}
    assert set(by) == {"mixed (imagery)", "mixed (elevation)"}
    names = {Path(i["path"]).name for i in by["mixed (imagery)"]["items"]}
    assert names == {"gray.tif", "rgb.tif", "rgba.tif", "img16.tif", "chip.ntf", "chip.jp2"}

    # Mixed band counts / dtypes / formats render together without crashing.
    src = LocalProduct(by["mixed (imagery)"])
    r = build_layer(src, BBox(-77.4, 38.1, -75.9, 38.9), LayerOptions(res_m=2000), OutputOptions(overviews=False),
                    settings.data_dir / "out", Context(settings), "mixed")
    assert r["status"] == "ok" and r["coverage_pct"] == 100.0


def test_rpf_and_ecrg_product_names():
    assert _rpf_product("NITF_TOC_ENTRY:CADRG_ONC_1:1M_1_1:/m/RPF/A.TOC") == "CADRG ONC 1:1M"
    assert _rpf_product("NITF_TOC_ENTRY:CIB_CIB05_5M_1_1:/m/RPF/A.TOC") == "CIB CIB05 5M"
    assert _ecrg_product("ECRG_TOC_ENTRY:ECRG:FalconView:1:500 K:/m/TOC.xml").startswith("ECRG")
