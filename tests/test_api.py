"""API hardening tests (offline)."""
from __future__ import annotations

import io
import json
import tarfile
import zipfile

import pytest
from fastapi.testclient import TestClient

from mapforge import sources
from mapforge.app import LOOPBACK_HOSTS, create_app
from mapforge.settings import Settings


@pytest.fixture()
def settings(tmp_path):
    s = Settings(data_dir=tmp_path / "data", library_dirs=[tmp_path / "lib"])
    sources.refresh(s)
    return s


@pytest.fixture()
def client(settings):
    return TestClient(create_app(settings, allowed_hosts=LOOPBACK_HOSTS), base_url="http://localhost:8765")


def test_job_delete_cannot_escape_jobs_dir(client, settings):
    (settings.config_dir / "keep.json").write_text("{}")
    for jid in ("..", "%2e%2e", "..%2fconfig"):
        assert client.delete(f"/api/jobs/{jid}").status_code in (404, 405)
    assert (settings.config_dir / "keep.json").exists() and settings.data_dir.exists()


def test_host_header_and_origin_guard(client):
    assert client.get("/api/info").status_code == 200
    assert client.get("/api/info", headers={"host": "evil.example"}).status_code == 403
    assert client.post("/api/library/rescan", headers={"origin": "http://evil.example"}).status_code == 403
    assert client.post("/api/library/rescan", headers={"origin": "null"}).status_code == 403
    assert client.post("/api/library/rescan", headers={"origin": "http://localhost:8765"}).status_code == 200


def test_token_required(settings):
    settings.token = "sekret"
    c = TestClient(create_app(settings))
    assert c.get("/api/info").status_code == 401
    assert c.get("/api/info", headers={"x-mapforge-token": "sekret"}).status_code == 200
    assert c.get("/api/info?token=sekret").status_code == 200
    assert c.get("/").status_code == 200  # static UI loads so it can prompt for the token


def _zip(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for k, v in members.items():
            z.writestr(k, v)
    return buf.getvalue()


def test_upload_rejects_zip_slip(client, settings, tmp_path):
    r = client.post("/api/library/upload", files={"file": ("evil.zip", _zip({"../../escape.txt": b"x"}))})
    assert r.status_code == 400
    assert not (tmp_path / "escape.txt").exists() and not (settings.data_dir / "escape.txt").exists()
    assert not (settings.library_dirs[0] / "evil").exists()
    assert not list((settings.library_dirs[0] / ".incoming").glob("*"))


def test_upload_tar_filter_blocks_traversal(client, settings, tmp_path):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        info = tarfile.TarInfo("../../escape.txt"); info.size = 1
        t.addfile(info, io.BytesIO(b"x"))
    r = client.post("/api/library/upload", files={"file": ("evil.tar", buf.getvalue())})
    assert r.status_code == 400 and not (tmp_path / "escape.txt").exists()


def test_upload_archive_gets_own_folder_and_sanitised_name(client, settings):
    r = client.post("/api/library/upload", files={"file": ("my NGA; disc.zip", _zip({"a/readme.txt": b"hi"}))})
    assert r.status_code == 200
    assert (settings.library_dirs[0] / "my NGA_ disc" / "a" / "readme.txt").exists()
    r = client.post("/api/library/upload", files={"file": ("..", b"x")})
    assert r.status_code == 200 and (settings.library_dirs[0] / "uploads" / "upload").exists()
    r = client.post("/api/library/upload", files={"file": ("bad.zip", b"not a zip")})
    assert r.status_code == 400


def test_endpoint_secrets_masked_and_preserved(client, settings):
    ep = {"name": "Svc", "type": "xyz", "url": "https://example.invalid/{z}/{x}/{y}.png",
          "auth": {"type": "basic", "username": "u", "password": "p@ss"}}
    assert client.post("/api/endpoints", json=ep).status_code == 200
    listed = client.get("/api/endpoints").json()["endpoints"][0]
    assert listed["auth"]["password"] == "********"
    listed["max_zoom"] = 12
    assert client.post("/api/endpoints", json=listed).status_code == 200
    stored = json.loads((settings.config_dir / "endpoints.json").read_text())[0]
    assert stored["auth"]["password"] == "p@ss" and stored["max_zoom"] == 12


def test_cannot_delete_running_job_and_validation(client, settings):
    bad = {"bbox": [0, 0, 1, 1], "layers": [{"source": "faa-heli"}, {"source": "faa-heli"}], "outputs": {}}
    assert client.post("/api/estimate", json=bad).status_code == 400
    bad["layers"] = [{"source": "faa-heli", "res_m": -5}]
    assert client.post("/api/estimate", json=bad).status_code == 400
