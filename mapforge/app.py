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


LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "[::1]", "::1"}
SAFE_NAME = re.compile(r"[^A-Za-z0-9._ -]+")


def _hostname(host_header: str) -> str:
    h = host_header.strip().lower()
    if h.startswith("["):
        return h[: h.find("]") + 1]
    return h.rsplit(":", 1)[0] if h.count(":") == 1 else h


def create_app(settings: Settings | None = None, allowed_hosts: set[str] | None = None) -> FastAPI:
    """allowed_hosts: Host-header names accepted (None = any). main() restricts this to loopback
    names when bound to 127.0.0.1, which defeats DNS-rebinding attacks from web pages."""
    if settings:
        set_settings(settings)
    s = get_settings()
    app = FastAPI(title="MapForge", version="0.1.0")
    jobs = JobManager(s)
    scan_state = {"running": False, "message": ""}

    @app.middleware("http")
    async def request_guard(request: Request, call_next):
        host = request.headers.get("host", "")
        if allowed_hosts is not None and _hostname(host) not in allowed_hosts:
            return JSONResponse({"detail": "Host not allowed"}, status_code=403)
        # CSRF: browsers always send Origin on cross-site POST/DELETE (incl. multipart forms,
        # which skip CORS preflight). Reject state changes coming from another site.
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if origin and origin != "null":
                from urllib.parse import urlsplit

                if urlsplit(origin).netloc.lower() != host.lower():
                    return JSONResponse({"detail": "Cross-origin request refused"}, status_code=403)
            elif origin == "null":
                return JSONResponse({"detail": "Cross-origin request refused"}, status_code=403)
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
        j = jobs.get(jid)
        if not j:
            raise HTTPException(404, "unknown job")
        if j["status"] in ("queued", "running"):
            raise HTTPException(409, "Cancel the job before deleting it")
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

        ep = dict(ep, auth=dict(ep.get("auth") or {}))
        old = next((e for e in s.load_json("endpoints.json", []) if e["id"] == ep.get("id")), None)
        for k, v in list(ep["auth"].items()):
            if v == "********":  # masked in the UI: use the stored secret
                ep["auth"][k] = ((old or {}).get("auth") or {}).get(k)
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
        name = SAFE_NAME.sub("_", Path(file.filename or "upload").name).strip(" .") or "upload"
        lib = s.library_dirs[0]
        staging = lib / ".incoming"
        staging.mkdir(parents=True, exist_ok=True)
        tmp = staging / (name + ".part")
        with open(tmp, "wb") as f:
            shutil.copyfileobj(file.file, f, 1 << 20)
        lower = name.lower()
        try:
            if lower.endswith((".zip", ".tar", ".tar.gz", ".tgz")):
                # Each archive gets its own top-level library folder, so it becomes its own
                # product(s) (the scanner groups loose rasters by top-level folder).
                stem = re.sub(r"\.(zip|tar|tar\.gz|tgz)$", "", name, flags=re.I).strip(" .") or "upload"
                target = lib / stem
                if lower.endswith(".zip"):
                    with zipfile.ZipFile(tmp) as z:
                        for m in z.namelist():
                            if Path(m).is_absolute() or ".." in Path(m).parts or ":" in m.split("/")[0]:
                                raise HTTPException(400, "archive contains unsafe paths")
                        target.mkdir(exist_ok=True)
                        z.extractall(target)
                else:
                    with tarfile.open(tmp) as t:
                        target.mkdir(exist_ok=True)
                        t.extractall(target, filter="data")
            else:
                (lib / "uploads").mkdir(exist_ok=True)
                tmp.replace(lib / "uploads" / name)
        except (zipfile.BadZipFile, tarfile.TarError) as e:
            raise HTTPException(400, f"not a valid archive: {e}")
        finally:
            tmp.unlink(missing_ok=True)
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
    loopback = a.host in ("127.0.0.1", "localhost", "::1")
    extra = {h.strip().lower() for h in os.environ.get("MAPFORGE_ALLOWED_HOSTS", "").split(",") if h.strip()}
    allowed = (LOOPBACK_HOSTS | extra) if (loopback or extra) else None
    if not loopback and not s.token:
        print("WARNING: serving on a non-loopback address without MAPFORGE_TOKEN — anyone who can reach "
              "this port can use the tool and your configured credentials.")
    uvicorn.run(create_app(s, allowed_hosts=allowed), host=a.host, port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
