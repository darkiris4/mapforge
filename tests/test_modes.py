"""Output modes: 'clipped' (cut in each file's own projection) and 'original' (files as published)."""
from __future__ import annotations

import http.server
import json
import threading
import time
from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.io import MemoryFile
from rasterio.transform import from_bounds
from rasterio.warp import transform_bounds

from mapforge import sources
from mapforge.geo import BBox
from mapforge.jobs import JobManager
from mapforge.process import LayerOptions, OutputOptions, _original_files, build_layer
from mapforge.settings import Settings
from mapforge.sources.base import Cancelled, Context
from mapforge.sources.local import scan
from mapforge.sources.services import TileService

ALBERS = "EPSG:5070"  # a projected CRS standing in for the FAA charts' Lambert projection
CMAP = {0: (0, 0, 0, 255), 1: (255, 0, 0, 255), 9: (255, 255, 255, 255)}


@pytest.fixture()
def settings(tmp_path):
    return Settings(data_dir=tmp_path / "data", library_dirs=[tmp_path / "lib"])


class FileSource(sources.Source):
    def __init__(self, paths, kind="rgb"):
        self.paths, self.kind = [Path(p) for p in paths], kind
        self.id, self.name, self.default_res_m, self.license = "test", "Test", 100.0, "test licence"

    def items(self, bbox, res_m, ctx):
        out = []
        for p in self.paths:
            with rasterio.open(p) as ds:
                fp = BBox(*transform_bounds(ds.crs, "EPSG:4326", *ds.bounds, densify_pts=21))
            if bbox.intersects(fp):
                out.append(sources.Item(path=str(p), footprint=fp, label=p.stem))
        return out


def write_projected_chart(path: Path, size=400, collar=60):
    """Paletted chart in a projected CRS with a white collar band around a red chart face."""
    # ~ -77.6..-76.4 lon, 38.4..39.3 lat in Albers metres
    left, bottom, right, top = 1_580_000.0, 1_880_000.0, 1_680_000.0, 1_980_000.0
    a = np.full((size, size), 9, np.uint8)
    a[collar:-collar, collar:-collar] = 1
    with rasterio.open(path, "w", driver="GTiff", width=size, height=size, count=1, dtype="uint8", crs=ALBERS,
                       transform=from_bounds(left, bottom, right, top, size, size), compress="deflate") as ds:
        ds.write(a, 1)
        ds.write_colormap(1, CMAP)
    with rasterio.open(path) as ds:
        return BBox(*transform_bounds(ds.crs, "EPSG:4326", *ds.bounds, densify_pts=21))


def write_dem(path: Path, bounds=(-78.0, 38.0, -77.0, 39.0), size=200):
    y, x = np.mgrid[0:size, 0:size]
    z = (100 + x * 0.5 + y).astype(np.float32)
    z[:5, :5] = -9999
    with rasterio.open(path, "w", driver="GTiff", width=size, height=size, count=1, dtype="float32",
                       crs="EPSG:4326", transform=from_bounds(*bounds, size, size), nodata=-9999,
                       compress="deflate") as ds:
        ds.write(z, 1)


def write_rgb(path: Path, bounds=(-78.0, 38.0, -77.0, 39.0), size=256, jpeg=False):
    rng = np.random.default_rng(1)
    a = rng.integers(0, 255, (3, size, size), dtype=np.uint8)
    prof = dict(driver="GTiff", width=size, height=size, count=3, dtype="uint8", crs="EPSG:4326",
                transform=from_bounds(*bounds, size, size), tiled=True, blockxsize=128, blockysize=128)
    prof.update(compress="jpeg", photometric="YCBCR") if jpeg else prof.update(compress="deflate")
    with rasterio.open(path, "w", **prof) as ds:
        ds.write(a)


class Recorder:
    def __init__(self):
        self.fracs, self.msgs = [], []

    def __call__(self, msg, frac=None):
        self.msgs.append(msg)
        if frac is not None:
            self.fracs.append(frac)


# ------------------------------------------------------------------------------ clipped
def test_clipped_keeps_projection_palette_and_collars(settings, tmp_path):
    chart = tmp_path / "Washington SEC.tif"
    fp = write_projected_chart(chart)
    # A box across the chart's western collar and into its face.
    box = BBox(fp.west + 0.05, fp.south + 0.3, fp.west + 0.5, fp.south + 0.6)
    rec = Recorder()
    res = build_layer(FileSource([chart]), box, LayerOptions(), OutputOptions(), tmp_path / "out",
                      Context(settings, progress=rec), "chart", mode="clipped")
    assert res["status"] == "ok" and res["mode"] == "clipped" and res["files"] == ["Washington SEC_clip.tif"]
    info = res["file_info"][0]
    assert info["palette"] and info["dtype"] == "uint8" and "EPSG:5070" in info["crs"]
    with rasterio.open(chart) as src, rasterio.open(tmp_path / "out" / "Washington SEC_clip.tif") as out:
        assert out.crs == src.crs and out.count == 1 and out.dtypes[0] == "uint8"
        assert out.colorinterp[0].name == "palette"
        assert {k: out.colormap(1)[k] for k in CMAP} == CMAP
        assert out.res == src.res  # not resampled
        b = transform_bounds(out.crs, "EPSG:4326", *out.bounds, densify_pts=21)
        assert b[0] <= box.west and b[1] <= box.south and b[2] >= box.east and b[3] >= box.north
        assert out.width < src.width and out.height < src.height
        vals = set(np.unique(out.read(1)).tolist())
        assert vals == {1, 9}  # chart face AND collar: nothing removed in this mode
        # Pixel-identical to the same window of the source.
        from rasterio.windows import from_bounds as fb
        win = fb(*out.bounds, transform=src.transform).round_offsets().round_lengths()
        assert np.array_equal(src.read(1, window=win), out.read(1))
    assert all(b >= a - 1e-9 for a, b in zip(rec.fracs, rec.fracs[1:])) and rec.fracs[-1] == pytest.approx(1)


def test_clipped_elevation_and_rgb_are_lossless(settings, tmp_path):
    dem, rgb, jpg = tmp_path / "dem.tif", tmp_path / "ortho.tif", tmp_path / "photo.tif"
    write_dem(dem)
    write_rgb(rgb)
    write_rgb(jpg, jpeg=True)
    box = BBox(-78.0, 38.7, -77.5, 39.0)  # touches the DEM's nodata corner (top-left)
    res = build_layer(FileSource([dem], kind="elevation"), box, LayerOptions(), OutputOptions(),
                      tmp_path / "e", Context(settings), "dem", mode="clipped")
    with rasterio.open(dem) as s, rasterio.open(tmp_path / "e" / res["files"][0]) as o:
        assert o.dtypes[0] == "float32" and o.nodata == -9999 and o.compression.value.upper() == "DEFLATE"
        win = rasterio.windows.from_bounds(*o.bounds, transform=s.transform).round_offsets().round_lengths()
        assert np.array_equal(s.read(1, window=win), o.read(1))
        assert (o.read(1) == -9999).any()
    res = build_layer(FileSource([rgb, jpg]), box, LayerOptions(), OutputOptions(), tmp_path / "r",
                      Context(settings), "rgb", mode="clipped")
    assert sorted(res["files"]) == ["ortho_clip.tif", "photo_clip.tif"]
    with rasterio.open(tmp_path / "r" / "ortho_clip.tif") as o, rasterio.open(rgb) as s:
        assert o.compression.value.upper() == "DEFLATE"
        win = rasterio.windows.from_bounds(*o.bounds, transform=s.transform).round_offsets().round_lengths()
        assert np.array_equal(s.read(window=win), o.read())
    with rasterio.open(tmp_path / "r" / "photo_clip.tif") as o:
        assert o.compression.value.upper() == "JPEG"  # already-lossy imagery stays JPEG


def test_clipped_skips_inputs_outside_box(settings, tmp_path):
    a, b = tmp_path / "a.tif", tmp_path / "b.tif"
    write_rgb(a, bounds=(-78.0, 38.0, -77.0, 39.0))
    write_rgb(b, bounds=(-77.0, 38.0, -76.0, 39.0))
    src = FileSource([a, b])
    res = build_layer(src, BBox(-77.9, 38.1, -77.5, 38.5), LayerOptions(), OutputOptions(), tmp_path / "o",
                      Context(settings), "x", mode="clipped")
    assert res["files"] == ["a_clip.tif"]


# ------------------------------------------------------------------------------ original
def test_original_copies_whole_files_with_sidecars(settings, tmp_path):
    folder = tmp_path / "faa"
    folder.mkdir()
    chart = folder / "Washington SEC.tif"
    write_projected_chart(chart)
    (folder / "Washington SEC.tfw").write_text("1\n0\n0\n-1\n0\n0\n")
    (folder / "Washington SEC.htm").write_text("<html>metadata</html>")
    write_rgb(folder / "Other.tif")  # unrelated neighbour, must not be copied
    with rasterio.open(chart) as ds:
        fp = BBox(*transform_bounds(ds.crs, "EPSG:4326", *ds.bounds))
    box = BBox(fp.west + 0.2, fp.south + 0.2, fp.west + 0.4, fp.south + 0.4)
    res = build_layer(FileSource([chart]), box, LayerOptions(), OutputOptions(), tmp_path / "out",
                      Context(settings), "chart", mode="original")
    assert res["mode"] == "original"
    assert sorted(res["files"]) == ["Washington SEC.htm", "Washington SEC.tfw", "Washington SEC.tif"]
    out = tmp_path / "out" / "Washington SEC.tif"
    assert out.read_bytes() == chart.read_bytes()  # whole file, byte-identical
    assert out.stat().st_ino == chart.stat().st_ino  # hard-linked: no extra disk used
    assert [f["file"] for f in res["file_info"]] == ["Washington SEC.tif"]
    assert res["file_info"][0]["width"] == 400


class _Handler(http.server.SimpleHTTPRequestHandler):
    tile_png = b""

    def log_message(self, *a):
        pass

    def do_GET(self):
        rng = self.headers.get("Range")
        if rng and ("," in rng or not rng.startswith("bytes=")):
            rng = None  # multi-range: answer with the whole file, which clients must accept
        if rng and not self.path.startswith("/tiles/"):  # GDAL /vsicurl/ reads with byte ranges
            data = (Path(self.directory) / self.path.lstrip("/")).read_bytes()
            a, _, b = rng.removeprefix("bytes=").partition("-")
            a, b = int(a), min(int(b) if b else len(data) - 1, len(data) - 1)
            self.send_response(206)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Range", f"bytes {a}-{b}/{len(data)}")
            self.send_header("Content-Length", str(b - a + 1))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            self.wfile.write(data[a:b + 1])
            return
        if self.path.startswith("/tiles/"):
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(self.tile_png)))
            self.end_headers()
            self.wfile.write(self.tile_png)
            return
        return super().do_GET()


@pytest.fixture()
def web(tmp_path):
    root = tmp_path / "www"
    root.mkdir()
    with MemoryFile() as mem:
        with mem.open(driver="PNG", width=256, height=256, count=3, dtype="uint8") as ds:
            ds.write(np.full((3, 256, 256), 150, np.uint8))
        _Handler.tile_png = mem.read()
    handler = lambda *a, **k: _Handler(*a, directory=str(root), **k)  # noqa: E731
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield root, f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_original_downloads_whole_remote_tile(settings, tmp_path, web):
    root, base = web
    write_dem(root / "tile.tif")

    class Remote(FileSource):
        def items(self, bbox, res_m, ctx):  # a partial remote (COG) read, like small DEM overlaps
            return [sources.Item(path=f"/vsicurl/{base}/tile.tif", footprint=BBox(-78, 38, -77, 39), label="tile",
                                 gdal_env={"GDAL_HTTP_MULTIRANGE": "SERIAL", "GDAL_HTTP_TIMEOUT": 10,
                                           "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR"})]

    res = build_layer(Remote([], kind="elevation"), BBox(-77.9, 38.1, -77.8, 38.2), LayerOptions(),
                      OutputOptions(), tmp_path / "o", Context(settings), "dem", mode="original")
    assert res["files"] == ["tile.tif"]
    assert (tmp_path / "o" / "tile.tif").read_bytes() == (root / "tile.tif").read_bytes()
    res = build_layer(Remote([], kind="elevation"), BBox(-77.9, 38.1, -77.8, 38.2), LayerOptions(),
                      OutputOptions(), tmp_path / "c", Context(settings), "dem", mode="clipped")
    assert res["files"] == ["tile_clip.tif"]
    with rasterio.open(tmp_path / "c" / "tile_clip.tif") as o:
        assert o.width < 30  # only the box, read remotely


@pytest.mark.parametrize("mode", ["clipped", "original"])
def test_tile_service_is_cut_in_its_native_projection(settings, tmp_path, web, mode):
    _, base = web
    svc = TileService("svc", "Test tiles", base + "/tiles/{z}/{x}/{y}.png", "Imagery", max_zoom=10,
                      default_res_m=150)
    box = BBox(-77.10, 38.85, -77.00, 38.95)
    res = build_layer(svc, box, LayerOptions(res_m=150), OutputOptions(), tmp_path / mode,
                      Context(settings), "svc", mode=mode)
    assert res["status"] == "ok" and len(res["files"]) == 1 and res["files"][0].endswith("_clip.tif")
    with rasterio.open(tmp_path / mode / res["files"][0]) as o:
        assert o.crs.to_epsg() == 3857 and o.count == 3 and o.dtypes[0] == "uint8"
        assert o.compression.value.upper() == "JPEG"
        b = transform_bounds(o.crs, "EPSG:4326", *o.bounds)
        assert b[0] <= box.west and b[2] >= box.east and b[1] <= box.south and b[3] >= box.north
        assert abs(o.transform.a - 152.87) < 1  # zoom 10: the level matching 150 m, not max zoom
        assert (o.read(1) == 150).mean() > 0.95


def test_rpf_products_copy_their_whole_tree(settings, tmp_path):
    toc = tmp_path / "disc" / "RPF" / "A.TOC"
    item = sources.Item(path=f"NITF_TOC_ENTRY:CADRG_ONC_1:1M_1_1:{toc}", footprint=BBox(0, 0, 1, 1))
    files, tree = _original_files(item, FileSource([]), Context(settings))
    assert files == [] and tree == toc.parent


def test_cancel_stops_clipping(settings, tmp_path):
    chart = tmp_path / "c.tif"
    fp = write_projected_chart(chart)
    ctx = Context(settings)
    ctx.cancel_event.set()
    with pytest.raises(Cancelled):
        build_layer(FileSource([chart]), fp, LayerOptions(), OutputOptions(), tmp_path / "o", ctx, "c",
                    mode="clipped")


# ------------------------------------------------------------------------------ jobs
def _run_job(jm: JobManager, spec: dict) -> dict:
    j = jm.submit(spec)
    for _ in range(300):
        if j["status"] not in ("queued", "running"):
            break
        time.sleep(0.05)
    return j


def test_job_modes_validation_manifest_and_readme(settings, tmp_path):
    lib = settings.library_dirs[0]
    (lib / "charts").mkdir(parents=True)
    fp = write_projected_chart(lib / "charts" / "chart.tif")
    (lib / "charts" / "chart.tfw").write_text("world file")
    scan(settings)
    sources.refresh(settings)
    jm = JobManager(settings)
    local = next(s for s in sources.registry(settings).values() if s.access == "local")
    box = [fp.west + 0.2, fp.south + 0.2, fp.west + 0.5, fp.south + 0.5]
    base = {"bbox": box, "layers": [{"source": local.id}]}
    with pytest.raises(ValueError, match="Unknown output mode"):
        jm.validate({**base, "mode": "bogus"})
    with pytest.raises(ValueError, match="output format"):
        jm.validate({**base, "outputs": {"geotiff": False}})  # kongsberg still needs a format
    for mode, title in [("clipped", "Clipped, not converted"), ("original", "Original files, untouched")]:
        j = _run_job(jm, {**base, "name": mode, "mode": mode, "outputs": {"geotiff": False}})
        assert j["status"] == "done", j
        assert j["spec"]["mode"] == mode
        pkg = Path(j["package"])
        manifest = json.loads((pkg / "manifest.json").read_text())
        assert manifest["mode"] == mode and "own projection" in manifest["crs"]
        layer = manifest["layers"][0]
        assert layer["mode"] == mode and layer["file_info"][0]["crs"].startswith("EPSG:5070")
        readme = (pkg / "README.txt").read_text()
        assert f"Mode: {title}" in readme and "projection EPSG:5070" in readme
        names = sorted(p.name for p in (pkg / layer["folder"]).iterdir())
        if mode == "original":
            assert names == ["chart.tfw", "chart.tif", "layer.json"]
            assert "alongside: chart.tfw" in readme
        else:
            assert names == ["chart_clip.tif", "layer.json"]
    # Kongsberg packages keep their README and gain only the mode in the manifest.
    j = _run_job(jm, {**base, "name": "k", "outputs": {"geotiff": True}})
    assert j["status"] == "done" and j["spec"]["mode"] == "kongsberg"
    pkg = Path(j["package"])
    assert json.loads((pkg / "manifest.json").read_text())["mode"] == "kongsberg"
    assert "All rasters are EPSG:4326" in (pkg / "README.txt").read_text()
