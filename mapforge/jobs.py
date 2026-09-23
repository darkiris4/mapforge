"""Background job runner: one job = one area, several layers, one package."""
from __future__ import annotations

import json
import re
import shutil
import threading
import time
import traceback
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .geo import BBox, grid_for
from .process import LayerOptions, OutputOptions, build_layer
from .settings import Settings
from .sources import Cancelled, Context, registry

# 96 dpi screen: one pixel ≈ 0.2646 mm, so a raster "looks native" at 1 : res_m / 0.0002646.
PX_M = 0.0002645833


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s).strip("_")[:60] or "area"


class JobManager:
    def __init__(self, settings: Settings):
        self.s = settings
        self.pool = ThreadPoolExecutor(settings.workers, thread_name_prefix="job")
        self.lock = threading.Lock()
        self.jobs: dict[str, dict] = {}
        self.cancel: dict[str, threading.Event] = {}
        for f in sorted(settings.jobs_dir.glob("*/job.json")):
            try:
                j = json.loads(f.read_text())
            except Exception:
                continue
            if j["status"] in ("queued", "running"):
                j["status"], j["message"] = "failed", "Interrupted by a server restart — resubmit the job."
            self.jobs[j["id"]] = j

    # -- persistence -------------------------------------------------------------------
    def _save(self, j: dict) -> None:
        d = self.s.jobs_dir / j["id"]
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / "job.json.tmp"
        tmp.write_text(json.dumps(j, indent=2))
        tmp.replace(d / "job.json")

    def list(self) -> list[dict]:
        return sorted(self.jobs.values(), key=lambda j: j["created"], reverse=True)

    def get(self, job_id: str) -> dict | None:
        return self.jobs.get(job_id)

    # -- validation / estimate -----------------------------------------------------------
    def validate(self, spec: dict) -> tuple[BBox, list[dict], OutputOptions]:
        bbox = BBox.from_any(spec["bbox"])
        reg = registry(self.s)
        layers = spec.get("layers") or []
        if not layers:
            raise ValueError("Select at least one layer")
        for layer in layers:
            if layer["source"] not in reg:
                raise ValueError(f"Unknown source {layer['source']}")
        o = spec.get("outputs") or {}
        outputs = OutputOptions(geotiff=o.get("geotiff", True), cog=o.get("cog", False),
                                mbtiles=o.get("mbtiles", False),
                                dted_level=None if o.get("dted_level") in (None, "", "none") else int(o["dted_level"]),
                                overviews=o.get("overviews", True))
        if not (outputs.geotiff or outputs.cog or outputs.mbtiles or outputs.dted_level is not None):
            raise ValueError("Choose at least one output format")
        return bbox, layers, outputs

    def estimate(self, spec: dict) -> dict:
        bbox, layers, outputs = self.validate(spec)
        reg = registry(self.s)
        rows = []
        for layer in layers:
            src = reg[layer["source"]]
            res = float(layer.get("res_m") or src.default_res_m)
            g = grid_for(bbox, res)
            bpp = 4 if src.kind == "elevation" else 3
            ratio = 0.08 if (src.kind == "rgb" and src.resampling != "nearest") else 0.3
            rows.append({"source": src.id, "name": src.name, "res_m": res, "width": g.width, "height": g.height,
                         "megapixels": round(g.pixels / 1e6, 1),
                         "est_mb": round(g.pixels * bpp * ratio * 1.33 / 1e6, 1),
                         "too_big": g.pixels > self.s.max_pixels})
        w, h = bbox.size_m()
        return {"area_km": [round(w / 1000, 1), round(h / 1000, 1)], "layers": rows,
                "total_mb": round(sum(r["est_mb"] for r in rows), 1)}

    # -- run ---------------------------------------------------------------------------
    def submit(self, spec: dict) -> dict:
        bbox, layers, outputs = self.validate(spec)
        jid = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        name = _safe(spec.get("name") or "area")
        j = {"id": jid, "name": name, "created": time.time(), "status": "queued", "message": "Queued",
             "progress": 0.0, "spec": {**spec, "bbox": list(bbox.as_tuple())}, "layers": [], "log": [],
             "package": None}
        with self.lock:
            self.jobs[jid] = j
            self.cancel[jid] = threading.Event()
        self._save(j)
        self.pool.submit(self._run, jid, bbox, layers, outputs)
        return j

    def cancel_job(self, jid: str) -> None:
        if jid in self.cancel:
            self.cancel[jid].set()

    def delete(self, jid: str) -> None:
        self.cancel_job(jid)
        self.jobs.pop(jid, None)
        shutil.rmtree(self.s.jobs_dir / jid, ignore_errors=True)

    def _run(self, jid: str, bbox: BBox, layers: list[dict], outputs: OutputOptions) -> None:
        j = self.jobs[jid]
        pkg = self.s.jobs_dir / jid / j["name"]
        pkg.mkdir(parents=True, exist_ok=True)
        j.update(status="running", package=str(pkg))
        last_save = [0.0]

        def progress(i: int, n: int):
            def cb(msg: str, frac: float | None = None):
                j["message"] = msg
                if frac is not None:
                    j["progress"] = round((i + frac) / n, 4)
                if not j["log"] or j["log"][-1][1] != msg:
                    if frac is None or frac in (0, 1):
                        j["log"] = (j["log"] + [[time.time(), msg]])[-200:]
                if time.time() - last_save[0] > 2:
                    last_save[0] = time.time()
                    self._save(j)
            return cb

        reg = registry(self.s)
        try:
            for i, layer in enumerate(layers):
                src = reg[layer["source"]]
                ctx = Context(self.s, progress(i, len(layers)), self.cancel[jid])
                lopts = LayerOptions(res_m=float(layer["res_m"]) if layer.get("res_m") else None,
                                     compression=layer.get("compression", "auto"))
                folder = f"{i + 1:02d}_{_safe(src.id)}"
                try:
                    r = build_layer(src, bbox, lopts, outputs, pkg / folder, ctx, _safe(src.id))
                except Cancelled:
                    raise
                except Exception as e:
                    r = {"source": src.id, "name": src.name, "status": "failed", "message": str(e),
                         "trace": traceback.format_exc(limit=4)}
                r["folder"] = folder
                j["layers"].append(r)
                j["progress"] = round((i + 1) / len(layers), 4)
                self._save(j)
            self._write_manifest(j, pkg, bbox)
            ok = [x for x in j["layers"] if x["status"] == "ok"]
            failed = [x for x in j["layers"] if x["status"] == "failed"]
            j["status"] = "done" if ok and not failed else "partial" if ok else "failed"
            j["message"] = (f"{len(ok)} layer(s) ready" + (f", {len(failed)} failed" if failed else "")
                            if ok else "No layers produced — see layer messages")
        except Cancelled:
            j.update(status="cancelled", message="Cancelled")
        except Exception as e:
            j.update(status="failed", message=str(e))
        j["finished"] = time.time()
        self._save(j)

    def _write_manifest(self, j: dict, pkg: Path, bbox: BBox) -> None:
        manifest = {"name": j["name"], "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(j["created"])),
                    "bbox_wsen": list(bbox.as_tuple()), "crs": "EPSG:4326 (WGS84 geographic)",
                    "generator": "MapForge", "layers": j["layers"]}
        (pkg / "manifest.json").write_text(json.dumps(manifest, indent=2))
        lines = [
            f"MapForge package: {j['name']}",
            f"Area (W,S,E,N): {', '.join(f'{v:.6f}' for v in bbox.as_tuple())}",
            "All rasters are EPSG:4326 (WGS84 lat/lon), tiled, with internal overviews.",
            "",
            "Layers (coarsest first — load in this order so finer layers draw on top):",
        ]
        ok = sorted([x for x in j["layers"] if x["status"] == "ok"], key=lambda x: -x["res_m"])
        for x in ok:
            native = x["res_m"] / PX_M
            lines += [f"  {x['folder']}/  {x['name']}",
                      f"      {x['res_m']} m/px, {x['width']}x{x['height']} px, coverage {x.get('coverage_pct', '?')}%",
                      f"      native display scale ≈ 1:{native:,.0f}; useful range ≈ 1:{native / 2:,.0f} – 1:{native * 8:,.0f}",
                      f"      files: {', '.join(x['files'][:6])}{' …' if len(x['files']) > 6 else ''}",
                      f"      licence: {x.get('license', '')}"]
        for x in j["layers"]:
            if x["status"] != "ok":
                lines.append(f"  (skipped) {x['name']}: {x.get('message', x['status'])}")
        lines += [
            "",
            "Loading into Kongsberg TerraLens:",
            "  * GeoTIFF / COG layers: add as raster (GeoTIFF) map layers; set each layer's visible scale",
            "    range to the 'useful range' above to get automatic chart switching as you zoom.",
            "  * dted/ folders use the standard DTED directory layout (dted/wNNN/nNN.dtL): point the",
            "    DTED/elevation data source at the dted/ folder.",
            "  * MBTiles (if produced) are Web-Mercator tile packages for web/mobile viewers.",
            "  * Check your TerraLens version's data-source documentation for any preprocessing/caching step",
            "    it applies to raster layers.",
            "",
            "Handling: observe the licence / distribution statement of every source above. Aeronautical",
            "charts are only current until their edition expires.",
        ]
        (pkg / "README.txt").write_text("\n".join(lines) + "\n")

    def zip_path(self, jid: str) -> Path:
        j = self.jobs[jid]
        pkg = Path(j["package"])
        z = pkg.with_suffix(".zip")
        if not z.exists() or z.stat().st_mtime < j.get("finished", 0):
            tmp = z.with_suffix(".zip.part")
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED, allowZip64=True) as zf:
                for f in sorted(pkg.rglob("*")):
                    if f.is_file():
                        zf.write(f, f.relative_to(pkg.parent))
            tmp.replace(z)
        return z
