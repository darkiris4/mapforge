"""Delivery: checksums, per-layer zips, export to folder, split for media (offline)."""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import time
import zipfile
from pathlib import Path

import numpy as np
import pytest
import rasterio
from fastapi.testclient import TestClient
from rasterio.transform import from_bounds

from mapforge import delivery, sources
from mapforge.app import LOOPBACK_HOSTS, create_app
from mapforge.jobs import JobManager
from mapforge.settings import Settings
from mapforge.sources.local import scan

HAVE_SHA256SUM = shutil.which("sha256sum") is not None


@pytest.fixture()
def settings(tmp_path):
    s = Settings(data_dir=tmp_path / "data", library_dirs=[tmp_path / "lib"])
    sources.refresh(s)
    return s


def fake_job(settings: Settings, jid: str = "20260924-120000-abc123", name: str = "PKG",
             sizes=(3_000_000, 1_000)) -> tuple[JobManager, dict, Path]:
    """A finished job on disk: two layer folders with pseudo-random files + README/manifest."""
    pkg = settings.jobs_dir / jid / name
    rng = np.random.default_rng(1)
    for n, (folder, size) in enumerate(zip(("01_alpha", "02_beta"), sizes)):
        (pkg / folder / "sub").mkdir(parents=True)
        (pkg / folder / f"layer{n}.tif").write_bytes(rng.bytes(size))
        (pkg / folder / "sub" / "x.dt1").write_bytes(rng.bytes(500))
        (pkg / folder / "layer.json").write_text("{}")
    (pkg / "README.txt").write_text("readme\n")
    (pkg / "manifest.json").write_text(json.dumps({"layers": []}))
    delivery.write_checksums(pkg)
    j = {"id": jid, "name": name, "created": time.time() - 10, "finished": time.time() - 5, "status": "done",
         "message": "ok", "progress": 1.0, "spec": {"bbox": [0, 0, 1, 1], "layers": []}, "layers": [],
         "log": [], "package": str(pkg), "deliveries": []}
    (settings.jobs_dir / jid / "job.json").write_text(json.dumps(j))
    jm = JobManager(settings)
    return jm, jm.get(jid), pkg


def client_for(settings) -> TestClient:
    return TestClient(create_app(settings, allowed_hosts=LOOPBACK_HOSTS), base_url="http://localhost:8765")


def wait(c: TestClient, jid: str, kind: str, timeout: float = 30) -> dict:
    """Poll the API (the app has its own JobManager) until the latest delivery of `kind` settles."""
    end = time.time() + timeout
    while time.time() < end:
        ds = [d for d in c.get(f"/api/jobs/{jid}").json().get("deliveries", []) if d["type"] == kind]
        if ds and ds[-1]["status"] != "running":
            return ds[-1]
        time.sleep(0.05)
    raise AssertionError(f"{kind} delivery did not finish")


# ---------------------------------------------------------------------------------- checksums
def test_checksums_cover_package_and_verify(settings):
    _, _, pkg = fake_job(settings)
    listed = [line.split("  ", 1)[1] for line in (pkg / "SHA256SUMS").read_text().splitlines()]
    assert "SHA256SUMS" not in listed and "01_alpha/sub/x.dt1" in listed and "README.txt" in listed
    assert delivery.verify_checksums(pkg) == (len(listed), [])
    if HAVE_SHA256SUM:
        subprocess.run(["sha256sum", "-c", "--quiet", "SHA256SUMS"], cwd=pkg, check=True)
    (pkg / "01_alpha" / "layer0.tif").write_bytes(b"tampered")
    ok, bad = delivery.verify_checksums(pkg)
    assert bad == ["01_alpha/layer0.tif: checksum mismatch"]


def test_real_job_writes_checksums(settings, tmp_path):
    lib = settings.library_dirs[0]
    with rasterio.open(lib / "chart.tif", "w", driver="GTiff", width=64, height=64, count=3, dtype="uint8",
                       crs="EPSG:4326", transform=from_bounds(-77, 38, -76, 39, 64, 64)) as ds:
        ds.write(np.full((3, 64, 64), 100, np.uint8))
    scan(settings)
    sources.refresh(settings)
    c = client_for(settings)
    src = next(x for x in c.get("/api/sources").json() if x["access"] == "local")
    jid = c.post("/api/jobs", json={"name": "t", "bbox": [-76.8, 38.2, -76.2, 38.8],
                                    "layers": [{"source": src["id"], "res_m": 2000}],
                                    "outputs": {"geotiff": True}}).json()["id"]
    for _ in range(200):
        j = c.get(f"/api/jobs/{jid}").json()
        if j["status"] not in ("queued", "running"):
            break
        time.sleep(0.05)
    assert j["status"] == "done" and j["deliveries"] == []
    pkg = Path(j["package"])
    ok, bad = delivery.verify_checksums(pkg)
    assert ok >= 4 and not bad  # tif, layer.json, manifest.json, README.txt


# ---------------------------------------------------------------------------- per-layer zips
def test_per_layer_download(settings):
    _, _, pkg = fake_job(settings)
    c = client_for(settings)
    jid = "20260924-120000-abc123"
    r = c.get(f"/api/jobs/{jid}/download", params={"layer": "02_beta"})
    assert r.status_code == 200
    names = set(zipfile.ZipFile(io.BytesIO(r.content)).namelist())
    assert "PKG/02_beta/layer1.tif" in names and "PKG/SHA256SUMS" in names and "PKG/README.txt" in names
    assert not any(n.startswith("PKG/01_alpha") for n in names)
    whole = zipfile.ZipFile(io.BytesIO(c.get(f"/api/jobs/{jid}/download").content)).namelist()
    assert "PKG/01_alpha/layer0.tif" in whole and "PKG/02_beta/layer1.tif" in whole
    for bad in ("..", "../01_alpha", "01_alpha/../..", "/etc", "SHA256SUMS", "nope", ".", "%2e%2e"):
        assert c.get(f"/api/jobs/{jid}/download", params={"layer": bad}).status_code == 404, bad


# ------------------------------------------------------------------------------------ export
def test_export_roots_default_and_env(tmp_path, monkeypatch):
    s = Settings(data_dir=tmp_path / "d", library_dirs=[tmp_path / "lib"])
    assert s.export_dirs == [tmp_path / "d" / "exports"] and (tmp_path / "d" / "exports").is_dir()
    monkeypatch.setenv("MAPFORGE_EXPORT_DIRS", os.pathsep.join([str(tmp_path / "a"), str(tmp_path / "b")]))
    s2 = Settings(data_dir=tmp_path / "d2", library_dirs=[tmp_path / "lib"])
    assert s2.export_dirs == [tmp_path / "a", tmp_path / "b"]
    sources.refresh(s2)
    r = client_for(s2).get("/api/export-roots").json()
    assert r == {"roots": [str(tmp_path / "a"), str(tmp_path / "b")], "default": str(tmp_path / "a")}


def test_export_copies_and_verifies(settings):
    jm, j, pkg = fake_job(settings)
    c = client_for(settings)
    jid = j["id"]
    root = settings.export_dirs[0]
    r = c.post(f"/api/jobs/{jid}/export", json={"dest": str(root / "share" / "maps")})
    assert r.status_code == 200, r.text
    d = wait(c, jid, "export")
    assert d["status"] == "done", d
    target = root / "share" / "maps" / "PKG"
    assert d["path"] == str(target) and "checksums verified" in d["message"]
    assert delivery.verify_checksums(target)[1] == []
    assert not [p for p in target.parent.iterdir() if p.name.startswith(".")]  # temp dir cleaned up
    # Existing non-empty target: refused without overwrite, replaced with it.
    assert c.post(f"/api/jobs/{jid}/export", json={"dest": str(root / "share" / "maps")}).status_code == 409
    (target / "stale.txt").write_text("old")
    r = c.post(f"/api/jobs/{jid}/export", json={"dest": str(root / "share" / "maps"), "overwrite": True})
    assert r.status_code == 200
    assert wait(c, jid, "export")["status"] == "done" and not (target / "stale.txt").exists()
    # Persisted in job.json and visible through the API.
    saved = json.loads((settings.jobs_dir / jid / "job.json").read_text())
    assert [d["type"] for d in saved["deliveries"]] == ["export", "export"]
    assert len(c.get(f"/api/jobs/{jid}").json()["deliveries"]) == 2


def test_export_path_validation(settings, tmp_path):
    jm, j, _ = fake_job(settings)
    c = client_for(settings)
    jid = j["id"]
    root = settings.export_dirs[0]
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "escape").symlink_to(outside)
    for dest in ("", "relative/dir", str(outside), "/etc", str(root / ".." / "x"), str(root / "escape"),
                 str(root / "escape" / "deeper")):
        r = c.post(f"/api/jobs/{jid}/export", json={"dest": dest})
        assert r.status_code == 400, (dest, r.text)
    # A symlink named like the package inside an allowed folder must not redirect the copy.
    (root / "ok").mkdir()
    (root / "ok" / "PKG").symlink_to(outside)
    assert c.post(f"/api/jobs/{jid}/export", json={"dest": str(root / "ok")}).status_code == 400
    assert list(outside.iterdir()) == [] and not c.get(f"/api/jobs/{jid}").json()["deliveries"]
    # Not ready / unknown job.
    assert c.post("/api/jobs/20260101-000000-000000/export", json={"dest": str(root)}).status_code == 404


def test_cross_site_delivery_refused(settings):
    _, j, _ = fake_job(settings)
    c = client_for(settings)
    for path, body in ((f"/api/jobs/{j['id']}/export", {"dest": str(settings.export_dirs[0])}),
                       (f"/api/jobs/{j['id']}/split", {"part_mb": 100})):
        assert c.post(path, json=body, headers={"origin": "http://evil.example"}).status_code == 403


# ------------------------------------------------------------------------------------- split
def test_split_join_and_verify(settings, monkeypatch, tmp_path):
    jm, j, pkg = fake_job(settings, sizes=(2_600_000, 1_000))
    c = client_for(settings)
    jid = j["id"]
    assert c.post(f"/api/jobs/{jid}/split", json={"part_mb": 49}).status_code == 400
    assert c.post(f"/api/jobs/{jid}/split", json={"part_mb": "x"}).status_code == 400
    monkeypatch.setattr(delivery, "MIN_PART_MB", 1)
    assert c.post(f"/api/jobs/{jid}/split", json={"part_mb": 1}).status_code == 200
    d = wait(c, jid, "split")
    assert d["status"] == "done", d
    parts = [f for f in d["files"] if ".zip." in f]
    assert parts == ["PKG.zip.001", "PKG.zip.002", "PKG.zip.003"]
    # Download every file through the API, join, verify.
    work = tmp_path / "media"
    work.mkdir()
    for f in d["files"]:
        r = c.get(f"/api/jobs/{jid}/parts/{f}")
        assert r.status_code == 200, f
        (work / f).write_bytes(r.content)
    readme = (work / "JOIN-README.txt").read_text()
    assert "cat PKG.zip.* > PKG.zip" in readme and "copy /b PKG.zip.001+PKG.zip.002+PKG.zip.003 PKG.zip" in readme
    assert "7-Zip" in readme
    with open(work / "PKG.zip", "wb") as out:
        for p in parts:
            out.write((work / p).read_bytes())
    assert delivery.verify_checksums(work) == (4, [])  # 3 parts + the joined zip
    if HAVE_SHA256SUM:
        subprocess.run(["sha256sum", "-c", "--quiet", "SHA256SUMS"], cwd=work, check=True)
    with zipfile.ZipFile(work / "PKG.zip") as zf:
        assert zf.testzip() is None and "PKG/01_alpha/layer0.tif" in zf.namelist()
    # Same size again: reused. Different size: old parts removed, one split entry left.
    assert c.post(f"/api/jobs/{jid}/split", json={"part_mb": 1}).json()["id"] == d["id"]
    c.post(f"/api/jobs/{jid}/split", json={"part_mb": 2})
    d2 = wait(c, jid, "split")
    assert d2["status"] == "done" and [f for f in d2["files"] if ".zip." in f] == ["PKG.zip.001", "PKG.zip.002"]
    assert sorted(p.name for p in (settings.jobs_dir / jid / "parts").iterdir()) == sorted(d2["files"])
    assert [x["id"] for x in c.get(f"/api/jobs/{jid}").json()["deliveries"] if x["type"] == "split"] == [d2["id"]]


def test_parts_route_rejects_traversal(settings):
    _, j, _ = fake_job(settings)
    c = client_for(settings)
    jid = j["id"]
    (settings.jobs_dir / jid / "parts").mkdir()
    (settings.jobs_dir / jid / "parts" / "ok.txt").write_text("hi")
    assert c.get(f"/api/jobs/{jid}/parts/ok.txt").text == "hi"
    # (a literal "/parts/.." is normalised by the client to /api/jobs/{id}, so test encoded forms)
    for bad in ("..%2Fjob.json", "%2e%2e", "job.json", "nope.txt", "..%2F..%2Fconfig", ".%2e"):
        assert c.get(f"/api/jobs/{jid}/parts/{bad}").status_code == 404, bad
    assert c.get(f"/api/jobs/{jid}/parts/..%2fjob.json").status_code == 404


def test_running_deliveries_fail_on_restart(settings):
    jm, j, _ = fake_job(settings)
    j["deliveries"] = [{"id": "x", "type": "split", "status": "running", "message": "Writing part 2 of 9…",
                        "path": None, "files": [], "created": time.time()}]
    jm._save(j)
    again = JobManager(settings).get(j["id"])
    assert again["deliveries"][0]["status"] == "failed" and "restart" in again["deliveries"][0]["message"]
