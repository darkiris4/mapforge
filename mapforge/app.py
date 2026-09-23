"""MapForge web API + static UI."""
from __future__ import annotations

import argparse
import re
import secrets
import shutil
import tarfile
import threading
import zipfile
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import sources
from .jobs import JobManager
from .settings import Settings, get_settings, set_settings
from .sources.base import Auth, Context
from .sources.custom import PRESETS, TYPES, endpoint_to_source
from .sources.local import scan

STATIC = Path(__file__).parent / "static"


def create_app(settings: Settings | None = None) -> FastAPI:
    if settings:
        set_settings(settings)
    s = get_settings()
    app = FastAPI(title="MapForge", version="0.1.0")
    jobs = JobManager(s)
    scan_state = {"running": False, "message": ""}

    @app.middleware("http")
    async def token_guard(request: Request, call_next):
        if s.token and request.url.path.startswith("/api/"):
            given = request.headers.get("x-mapforge-token") or request.query_params.get("token") or ""
            if not secrets.compare_digest(given, s.token):
                return JSONResponse({"detail": "Missing or bad token"}, status_code=401)
        return await call_next(request)

    def bad(e: Exception):
        raise HTTPException(400, str(e))

    # -- sources ---------------------------------------------------------------------------
    @app.get("/api/sources")
    def list_sources():
        return [src.info() for src in sources.registry(s).values()]

    @app.get("/api/sources/{sid}/coverage")
    def coverage(sid: str):
        src = sources.registry(s).get(sid)
        if not src:
            raise HTTPException(404, "unknown source")
        try:
            return src.coverage(Context(s))
        except Exception as e:
            raise HTTPException(502, f"Could not load coverage: {e}")

    # -- jobs ------------------------------------------------------------------------------
    @app.post("/api/estimate")
    def estimate(spec: dict):
        try:
            return jobs.estimate(spec)
        except (ValueError, KeyError) as e:
            bad(e)

    @app.post("/api/jobs")
    def submit(spec: dict):
        try:
            return jobs.submit(spec)
        except (ValueError, KeyError) as e:
            bad(e)

    @app.get("/api/jobs")
    def list_jobs():
        return [{k: v for k, v in j.items() if k != "log"} for j in jobs.list()]

    @app.get("/api/jobs/{jid}")
    def get_job(jid: str):
        j = jobs.get(jid)
        if not j:
            raise HTTPException(404, "unknown job")
        return j

    @app.post("/api/jobs/{jid}/cancel")
    def cancel(jid: str):
        jobs.cancel_job(jid)
        return {"ok": True}

    @app.delete("/api/jobs/{jid}")
    def delete(jid: str):
        jobs.delete(jid)
        return {"ok": True}

    @app.get("/api/jobs/{jid}/download")
    def download(jid: str):
        j = jobs.get(jid)
        if not j or j["status"] not in ("done", "partial"):
            raise HTTPException(404, "package not ready")
        z = jobs.zip_path(jid)
        return FileResponse(z, filename=z.name, media_type="application/zip")

    # -- custom endpoints (NGA / PKI / commercial) -------------------------------------------
    @app.get("/api/endpoints")
    def endpoints():
        eps = s.load_json("endpoints.json", [])
        return {"types": TYPES, "presets": PRESETS,
                "endpoints": [{**e, "auth": Auth.from_dict(e.get("auth")).public()} for e in eps]}

    @app.post("/api/endpoints")
    def save_endpoint(ep: dict):
        for k in ("name", "type", "url"):
            if not ep.get(k):
                bad(ValueError(f"'{k}' is required"))
        if ep["type"] not in TYPES:
            bad(ValueError("unknown endpoint type"))
        eps = s.load_json("endpoints.json", [])
        ep["id"] = ep.get("id") or re.sub(r"[^a-z0-9]+", "-", ep["name"].lower()).strip("-")
        old = next((e for e in eps if e["id"] == ep["id"]), None)
        if old:  # keep stored secrets when the UI sends back the masked value
            for k, v in (ep.get("auth") or {}).items():
                if v == "********":
                    ep["auth"][k] = (old.get("auth") or {}).get(k)
        auth = ep.get("auth") or {}
        for k in ("cert", "key", "ca_bundle"):
            if auth.get(k) and not Path(auth[k]).expanduser().exists():
                bad(ValueError(f"{k} file not found on the server: {auth[k]}"))
        try:
            endpoint_to_source(ep)
        except Exception as e:
            bad(e)
        eps = [e for e in eps if e["id"] != ep["id"]] + [ep]
        s.save_json("endpoints.json", eps)
        sources.refresh(s)
        return {"ok": True, "id": ep["id"]}

    @app.delete("/api/endpoints/{eid}")
    def delete_endpoint(eid: str):
        s.save_json("endpoints.json", [e for e in s.load_json("endpoints.json", []) if e["id"] != eid])
        sources.refresh(s)
        return {"ok": True}

    @app.post("/api/endpoints/test")
    def test_endpoint(ep: dict):
        """Open the endpoint through GDAL for a tiny area to prove connectivity + auth."""
        import rasterio

        try:
            src = endpoint_to_source({**ep, "id": ep.get("id") or "test"})
            from .geo import BBox

            b = BBox.from_any(ep.get("test_bbox") or [-77.04, 38.88, -77.02, 38.90])
            items = src.items(b, max(src.default_res_m, 5), Context(s))
            if not items:
                return {"ok": False, "message": "No data returned for the test area"}
            with rasterio.Env(**items[0].gdal_env), rasterio.open(items[0].path) as ds:
                return {"ok": True, "message": f"OK — {ds.width}x{ds.height}, {ds.count} band(s), {ds.crs}"}
        except Exception as e:
            return {"ok": False, "message": str(e)[:500]}

    # -- local library -----------------------------------------------------------------------
    def _rescan():
        scan_state.update(running=True, message="Scanning…")
        try:
            idx = scan(s, lambda m: scan_state.update(message=m))
            scan_state["message"] = f"Found {len(idx['products'])} product(s)"
        except Exception as e:
            scan_state["message"] = f"Scan failed: {e}"
        finally:
            sources.refresh(s)
            scan_state["running"] = False

    @app.get("/api/library")
    def library():
        idx = s.load_json("library_index.json", {"products": []})
        return {"dirs": [str(d) for d in s.library_dirs], "scan": scan_state,
                "products": [{"id": p["id"], "name": p["name"], "kind": p["kind"], "files": len(p["items"])}
                             for p in idx.get("products", [])]}

    @app.post("/api/library/rescan")
    def rescan():
        if not scan_state["running"]:
            threading.Thread(target=_rescan, daemon=True).start()
        return scan_state

    @app.post("/api/library/upload")
    def upload(file: UploadFile = File(...)):
        """Upload NGA media (zip/tar of an RPF tree, DTED, NITF, GeoTIFF …) into the library."""
        name = Path(file.filename or "upload").name
        dest_root = s.library_dirs[0] / "uploads"
        dest_root.mkdir(parents=True, exist_ok=True)
        tmp = dest_root / (name + ".part")
        with open(tmp, "wb") as f:
            shutil.copyfileobj(file.file, f, 1 << 20)
        lower = name.lower()
        if lower.endswith(".zip") or lower.endswith((".tar", ".tar.gz", ".tgz")):
            target = dest_root / re.sub(r"\.(zip|tar|tar\.gz|tgz)$", "", name, flags=re.I)
            target.mkdir(exist_ok=True)
            try:
                if lower.endswith(".zip"):
                    with zipfile.ZipFile(tmp) as z:
                        for m in z.namelist():
                            if Path(m).is_absolute() or ".." in Path(m).parts:
                                raise HTTPException(400, "archive contains unsafe paths")
                        z.extractall(target)
                else:
                    with tarfile.open(tmp) as t:
                        t.extractall(target, filter="data")
            finally:
                tmp.unlink(missing_ok=True)
        else:
            tmp.replace(dest_root / name)
        rescan()
        return {"ok": True}

    @app.get("/api/info")
    def info():
        return {"data_dir": str(s.data_dir), "library_dirs": [str(d) for d in s.library_dirs],
                "max_pixels": s.max_pixels, "token_required": bool(s.token)}

    app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
    return app


def main() -> None:
    import os

    ap = argparse.ArgumentParser(description="MapForge — self-hosted map fetch / bound / convert tool")
    ap.add_argument("--host", default=os.environ.get("MAPFORGE_HOST", "127.0.0.1"),
                    help="bind address (default 127.0.0.1; use 0.0.0.0 to serve your LAN)")
    ap.add_argument("--port", type=int, default=int(os.environ.get("MAPFORGE_PORT", "8765")))
    ap.add_argument("--data", help="data directory (cache, jobs, config)")
    ap.add_argument("--library", action="append", help="local library directory (repeatable)")
    a = ap.parse_args()
    kw = {}
    if a.data:
        kw["data_dir"] = Path(a.data).expanduser().resolve()
    if a.library:
        kw["library_dirs"] = [Path(p).expanduser().resolve() for p in a.library]
    s = Settings(**kw)
    import uvicorn

    print(f"MapForge on http://{a.host}:{a.port}  data={s.data_dir}  library={', '.join(map(str, s.library_dirs))}")
    uvicorn.run(create_app(s), host=a.host, port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
