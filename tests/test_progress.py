"""Progress reporting: the job bar must move while inputs download, not only while rendering."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass

import numpy as np
import pytest
import rasterio
from rasterio.io import MemoryFile
from rasterio.transform import from_bounds

from mapforge import sources
from mapforge.geo import BBox
from mapforge.process import FETCH_SHARE, LayerOptions, OutputOptions, build_layer
from mapforge.settings import Settings
from mapforge.sources.base import Context
from mapforge.sources.services import ArcGISImageServer


@pytest.fixture()
def settings(tmp_path):
    return Settings(data_dir=tmp_path / "data", library_dirs=[tmp_path / "lib"])


def tiff_bytes(bbox: BBox, w: int, h: int) -> bytes:
    with MemoryFile() as mem:
        with mem.open(driver="GTiff", width=w, height=h, count=3, dtype="uint8", crs="EPSG:4326",
                      transform=from_bounds(*bbox.as_tuple(), w, h)) as ds:
            ds.write(np.full((3, h, w), 120, np.uint8))
        return mem.read()


class Recorder:
    def __init__(self):
        self.events: list[tuple[float, str, float | None]] = []
        self.lock = threading.Lock()

    def __call__(self, msg, frac=None):
        with self.lock:
            self.events.append((time.monotonic(), msg, frac))

    @property
    def fracs(self):
        return [f for _, _, f in self.events if f is not None]


class SlowFirstChunkServer:
    """Fake httpx client for exportImage: the first chunk requested is slow."""

    def __init__(self, slow_s: float):
        self.slow_s, self.first = slow_s, True
        self.lock = threading.Lock()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, params=None):
        with self.lock:
            slow, self.first = self.first, False
        if slow:
            time.sleep(self.slow_s)
        w, h = (int(v) for v in params["size"].split(","))
        b = BBox(*(float(v) for v in params["bbox"].split(",")))

        @dataclass
        class R:
            content: bytes
            status_code: int = 200
            headers = {"content-type": "image/tiff"}

            def raise_for_status(self):
                pass

        return R(tiff_bytes(b, w, h))


class FakeHttpContext(Context):
    server: SlowFirstChunkServer | None = None

    def http(self, auth=None, timeout=60):
        return self.server


def test_export_chunks_report_in_completion_order(settings, monkeypatch):
    monkeypatch.setattr(ArcGISImageServer, "CHUNK", 50)
    src = ArcGISImageServer("t", "T", "https://example.invalid/ImageServer", "g", default_res_m=10)
    bbox = BBox(0.0, 0.0, 0.02, 0.02)  # ~2.2 km at 10 m -> 223 px -> 5x5 = 25 chunks of 50 px
    assert src.chunk_count(bbox, 10) == 25
    rec = Recorder()
    ctx = FakeHttpContext(settings, progress=rec)
    ctx.server = SlowFirstChunkServer(slow_s=1.0)
    t0 = time.monotonic()
    items = src.items(bbox, 10, ctx)
    assert len(items) == 25
    fetched = [(t, f) for t, m, f in rec.events if "fetched" in m]
    assert [round(f * 25) for _, f in fetched] == list(range(1, 26))  # 1/25 … 25/25, monotonic
    # Chunks behind the slow first one are counted as they land, not after it.
    assert fetched[0][0] - t0 < 0.5


def test_layer_bar_moves_during_fetch_and_is_monotonic(settings, tmp_path):
    paths = []
    for i in range(4):
        p = tmp_path / f"img{i}.tif"
        b = BBox(i * 0.25, 0.0, (i + 1) * 0.25, 0.25)
        with rasterio.open(p, "w", driver="GTiff", width=64, height=64, count=3, dtype="uint8",
                           crs="EPSG:4326", transform=from_bounds(*b.as_tuple(), 64, 64)) as ds:
            ds.write(np.full((3, 64, 64), 90, np.uint8))
        paths.append(p)

    class SlowFetchSource(sources.Source):
        id, name, kind, default_res_m, resampling = "slow", "Slow", "rgb", 500.0, "bilinear"

        def items(self, bbox, res_m, ctx):
            out = []
            for n, p in enumerate(paths):
                ctx.progress(f"downloading {n + 1}/{len(paths)}", n / len(paths))
                with rasterio.open(p) as ds:
                    out.append(sources.Item(path=str(p), footprint=BBox(*ds.bounds)))
            ctx.progress("downloaded", 1.0)
            return out

    rec = Recorder()
    res = build_layer(SlowFetchSource(), BBox(0.0, 0.0, 1.0, 0.25), LayerOptions(), OutputOptions(),
                      tmp_path / "out", Context(settings, progress=rec), "slow")
    assert res["status"] == "ok"
    fracs = rec.fracs
    fetch = [f for _, m, f in rec.events if f is not None and m.startswith("download")]
    assert fetch and all(0.0 <= f <= FETCH_SHARE + 1e-9 for f in fetch)
    assert 0.0 < fetch[1] < FETCH_SHARE  # the bar moves while inputs are still downloading
    assert all(b >= a - 1e-9 for a, b in zip(fracs, fracs[1:])), fracs
    assert fracs[-1] == pytest.approx(1.0)


def test_estimate_reports_export_requests(settings):
    from mapforge.jobs import EXPORT_CHUNKS_PER_MIN, JobManager

    sources.refresh(settings)
    jm = JobManager(settings)
    # The El Paso NAIP box that took ~3 h at the default 1 m resolution.
    spec = {"bbox": [-106.676559, 31.632948, -106.191101, 31.871822], "layers": [{"source": "usgs-naip"}],
            "outputs": {"geotiff": True}}
    row = jm.estimate(spec)["layers"][0]
    assert row["requests"] >= 300 and row["fetch_minutes"] == round(row["requests"] / EXPORT_CHUNKS_PER_MIN)
    spec["layers"][0]["res_m"] = 4
    assert jm.estimate(spec)["layers"][0]["requests"] <= row["requests"] / 12
