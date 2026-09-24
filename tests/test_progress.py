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


class TimeoutOnBigExports(SlowFirstChunkServer):
    """Returns 504 for exports whose edge is >= limit px (a busy ImageServer), else a TIFF."""

    def __init__(self, limit: int):
        super().__init__(slow_s=0)
        self.limit, self.calls = limit, []

    def get(self, url, params=None):
        w, h = (int(v) for v in params["size"].split(","))
        self.calls.append((w, h))
        if max(w, h) >= self.limit:
            class R504:
                status_code = 504
                headers = {"content-type": "text/html"}
                content = b"<html>504 Gateway Time-out</html>"
            return R504()
        return super().get(url, params)


def test_export_splits_on_gateway_timeout_and_resumes(settings, monkeypatch):
    monkeypatch.setattr(ArcGISImageServer, "CHUNK", 2000)
    monkeypatch.setattr(ArcGISImageServer, "MIN_SPLIT", 500)
    monkeypatch.setattr(ArcGISImageServer, "RETRY_DELAYS_S", (0, 0, 0))
    src = ArcGISImageServer("t", "T", "https://example.invalid/ImageServer", "g", default_res_m=1)
    bbox = BBox(0.0, 0.0, 0.017, 0.017)  # ~1890 px at 1 m -> a single chunk
    assert src.chunk_count(bbox, 1) == 1
    ctx = FakeHttpContext(settings, progress=Recorder())
    ctx.server = TimeoutOnBigExports(limit=1000)
    items = src.items(bbox, 1, ctx)
    assert len(items) == 4  # 1 chunk -> quartered after 3 x 504
    assert sum(1 for c in ctx.server.calls if max(c) >= 1000) == 3
    # Quarters tile the chunk exactly.
    assert min(i.footprint.west for i in items) == 0.0 and max(i.footprint.east for i in items) == 0.017
    # Re-run: goes straight to the cached quarters, never re-asks for the big export.
    ctx.server = TimeoutOnBigExports(limit=1000)
    assert len(src.items(bbox, 1, ctx)) == 4 and ctx.server.calls == []


def test_export_failure_says_how_much_is_cached(settings, monkeypatch):
    monkeypatch.setattr(ArcGISImageServer, "CHUNK", 400)
    monkeypatch.setattr(ArcGISImageServer, "MIN_SPLIT", 500)  # 400 px chunks can't be split further
    monkeypatch.setattr(ArcGISImageServer, "RETRY_DELAYS_S", (0, 0))
    monkeypatch.setattr(ArcGISImageServer, "WORKERS", 1)
    src = ArcGISImageServer("t", "T", "https://example.invalid/ImageServer", "g", default_res_m=1)
    bbox = BBox(0.0, 0.0, 0.0071, 0.0035)  # ~790x390 px -> 2 chunks of <=400 px side by side
    assert src.chunk_count(bbox, 1) == 2

    class FailSecond(TimeoutOnBigExports):
        def get(self, url, params=None):
            self.calls.append(params["bbox"])
            return super().get(url, params) if len(set(self.calls)) == 1 else \
                type("R", (), {"status_code": 504, "headers": {}, "content": b""})()

    ctx = FakeHttpContext(settings, progress=Recorder())
    ctx.server = FailSecond(limit=10_000)
    with pytest.raises(RuntimeError, match=r"1/2 chunks are cached; run the job again"):
        src.items(bbox, 1, ctx)
