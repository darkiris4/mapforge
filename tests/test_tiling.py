"""Auto-tiling: a too-big layer is split into independently-built pieces instead of failing
the whole job (see JobManager._build_tiled / _build_tile_worker / _merge_tiles in
mapforge/jobs.py).

Tiles build in separate OS processes now, not threads (see the TILE_WORKERS comment in jobs.py
for why) — every real end-to-end test here goes through JobManager.submit()/a real subprocess,
using a local-library source (files on disk, so a freshly-spawned child process can rediscover
it via registry(settings) exactly the way the real server does) rather than mocking build_layer
or hand-injecting a fake Source into the registry — neither survives a process boundary, since a
'spawn'-started child re-imports everything fresh and never sees the parent's monkeypatches or
in-memory dict mutations. Pure aggregation/math (_merge_tiles, the grid-sizing formula) is
tested directly against synthetic inputs instead, since that logic needs no subprocess at all.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_bounds

from mapforge import sources
from mapforge.geo import BBox, grid_for, split_bbox
from mapforge.jobs import JobManager, _safe, _tile_grid_size
from mapforge.settings import Settings
from mapforge.sources.base import Context, TooManyTiles
from mapforge.sources.local import scan


@pytest.fixture()
def settings(tmp_path):
    return Settings(data_dir=tmp_path / "data", library_dirs=[tmp_path / "lib"])


def write_rgb(path: Path, bounds, size=256):
    rng = np.random.default_rng(1)
    a = rng.integers(0, 255, (3, size, size), dtype=np.uint8)
    with rasterio.open(path, "w", driver="GTiff", width=size, height=size, count=3, dtype="uint8",
                       crs="EPSG:4326", transform=from_bounds(*bounds, size, size), compress="deflate") as ds:
        ds.write(a)


def _run_job(jm: JobManager, spec: dict) -> dict:
    j = jm.submit(spec)
    for _ in range(1200):
        if j["status"] not in ("queued", "running"):
            break
        time.sleep(0.1)
    return j


def _local_source_id(settings) -> str:
    sources.refresh(settings)
    return next(s.id for s in sources.registry(settings).values() if s.access == "local")


# -------------------------------------------------------------------------- split_bbox geometry
@pytest.mark.parametrize("cols,rows", [(1, 1), (2, 2), (3, 1), (1, 4), (4, 3)])
def test_split_bbox_tiles_exactly_with_no_gap_or_overlap(cols, rows):
    bbox = BBox(-10.0, 5.0, 14.0, 21.0)
    subs = split_bbox(bbox, cols, rows)
    assert len(subs) == cols * rows
    total = sum((s.east - s.west) * (s.north - s.south) for s in subs)
    assert total == pytest.approx((bbox.east - bbox.west) * (bbox.north - bbox.south))
    for s in subs:
        assert bbox.west - 1e-9 <= s.west < s.east <= bbox.east + 1e-9
        assert bbox.south - 1e-9 <= s.south < s.north <= bbox.north + 1e-9
    for r in range(rows):
        row = sorted(subs[r * cols:(r + 1) * cols], key=lambda s: s.west)
        for a, b in zip(row, row[1:]):
            assert a.east == pytest.approx(b.west)


@pytest.mark.parametrize("factor,expect", [(1.0, (2, 1)), (4.0, (2, 2)), (4.4, (3, 2)), (8.0, (3, 3)), (9.0, (3, 3))])
def test_tile_grid_size_from_factor(factor, expect):
    assert _tile_grid_size(factor) == expect


# -------------------------------------------------------------------------- end-to-end (real subprocesses)
def test_kongsberg_tiling_reconstructs_the_full_area_and_resumes(settings, tmp_path):
    """One real job, through the real HTTP-shaped submit()/subprocess path: proves the tiles'
    real on-disk bounds exactly reconstruct the requested area (no coordinate mixup between the
    grid math and what actually gets written), then proves resumability by deleting the source
    raster and re-tiling into the *same* output folder — if the marker-file skip didn't work,
    this second pass would try to rebuild from the now-missing file and fail."""
    bounds = (-2.0, -2.0, 2.0, 2.0)
    lib = settings.library_dirs[0]
    lib.mkdir(parents=True, exist_ok=True)
    src_path = lib / "big.tif"
    write_rgb(src_path, bounds, size=512)
    bbox = BBox(*bounds)
    res_m = 1000.0
    full_pixels = grid_for(bbox, res_m).pixels
    settings.max_pixels = full_pixels // 6  # forces a multi-tile split, no recursion needed

    source_id = _local_source_id(settings)
    jm = JobManager(settings)
    spec = {"bbox": list(bounds), "layers": [{"source": source_id, "res_m": res_m}],
            "outputs": {"geotiff": True}, "mode": "kongsberg", "name": "tiled"}
    j = _run_job(jm, spec)
    assert j["status"] == "done", j
    layer = j["layers"][0]
    assert layer["status"] == "ok"
    assert "tiles" in layer and len(layer["tiles"]) > 1
    pkg = Path(j["package"])

    seen_area = 0.0
    for t in layer["tiles"]:
        tb = BBox(*t["bbox"])
        tif = next((pkg / layer["folder"] / t["folder"]).glob("*.tif"))
        with rasterio.open(tif) as ds:
            got = BBox(*ds.bounds)
        assert got.west == pytest.approx(tb.west, abs=1e-4) and got.east == pytest.approx(tb.east, abs=1e-4)
        assert got.south == pytest.approx(tb.south, abs=1e-4) and got.north == pytest.approx(tb.north, abs=1e-4)
        seen_area += (tb.east - tb.west) * (tb.north - tb.south)
    assert seen_area == pytest.approx((bbox.east - bbox.west) * (bbox.north - bbox.south))

    manifest = json.loads((pkg / "manifest.json").read_text())
    assert manifest["layers"][0]["tiles"]
    readme = (pkg / "README.txt").read_text()
    assert "split into" in readme and "tile(s)" in readme

    # -- resumability: same output folder, source file gone -----------------------------------
    src_path.unlink()
    src = sources.registry(settings)[source_id]
    r2 = jm._build_tiled(src, bbox, spec_layer_options(res_m), outputs_from(spec), pkg / layer["folder"],
                         Context(settings), _safe(source_id), "kongsberg", None)
    assert r2["status"] == "ok"
    assert len(r2["tiles"]) == len(layer["tiles"])  # every tile reused from its layer.json, none rebuilt


def spec_layer_options(res_m):
    from mapforge.process import LayerOptions
    return LayerOptions(res_m=res_m)


def outputs_from(spec):
    from mapforge.process import OutputOptions
    o = spec["outputs"]
    return OutputOptions(geotiff=o.get("geotiff", True))


# -------------------------------------------------------------------------- _merge_tiles (pure, no subprocess)
def _fake_source():
    return SimpleNamespace(id="s", name="S", license="lic", kind="rgb")


def _ok(**extra):
    return {"status": "ok", "files": ["a.tif"], "res_m": 10.0, "kind": "rgb", **extra}


def test_merge_tiles_keeps_layer_ok_when_some_tiles_fail():
    src = _fake_source()
    subs = split_bbox(BBox(0, 0, 4, 4), 2, 2)
    results = [_ok(), _ok(), {"status": "failed", "message": "server dropped the connection"}, _ok()]
    r = JobManager._merge_tiles(src, BBox(0, 0, 4, 4), "kongsberg", subs, results)
    assert r["status"] == "ok", r
    assert "1 failed" in r["summary"]
    assert len(r["files"]) == 3  # only the 3 successful tiles' files, correctly tile-prefixed
    assert r["files"] == ["tile_00/a.tif", "tile_01/a.tif", "tile_03/a.tif"]
    statuses = sorted(t["status"] for t in r["tiles"])
    assert statuses == ["failed", "ok", "ok", "ok"]


def test_merge_tiles_all_failed_reports_failed_not_empty():
    src = _fake_source()
    subs = split_bbox(BBox(0, 0, 4, 4), 2, 1)
    results = [{"status": "failed", "message": "boom"}, {"status": "failed", "message": "boom"}]
    r = JobManager._merge_tiles(src, BBox(0, 0, 4, 4), "kongsberg", subs, results)
    assert r["status"] == "failed" and r["message"] == "boom"


def test_merge_tiles_all_empty_reports_empty():
    src = _fake_source()
    subs = split_bbox(BBox(0, 0, 4, 4), 2, 1)
    results = [{"status": "empty"}, {"status": "empty"}]
    r = JobManager._merge_tiles(src, BBox(0, 0, 4, 4), "kongsberg", subs, results)
    assert r["status"] == "empty"


def test_merge_tiles_preserves_original_mode():
    """The real-world case that motivated this: an XYZ imagery source in Original mode raising
    TooManyTiles from its own items() call, not from the kongsberg pixel-grid check — merge
    logic must carry the mode through untouched either way."""
    src = _fake_source()
    subs = split_bbox(BBox(0, 0, 2, 2), 3, 3)
    results = [_ok(mode="original") for _ in range(9)]
    r = JobManager._merge_tiles(src, BBox(0, 0, 2, 2), "original", subs, results)
    assert r["status"] == "ok" and r["mode"] == "original"
    assert len(r["tiles"]) == 9 and len(r["files"]) == 9
