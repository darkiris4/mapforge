"""Background job runner: one job = one area, several layers, one package."""
from __future__ import annotations

import json
import math
import re
import shutil
import threading
import time
import traceback
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import delivery
from .geo import BBox, grid_for
from .process import MODES, LayerOptions, OutputOptions, build_layer
from .settings import Settings
from .sources import Cancelled, Context, registry

# 96 dpi screen: one pixel ≈ 0.2646 mm, so a raster "looks native" at 1 : res_m / 0.0002646.
PX_M = 0.0002645833
# Observed throughput of USGS ImageServer exports (2000x2000 px, 4 in parallel): ~2 per minute.
# (Estimates use the learned rate from mapforge.speeds once a server has been measured.)
EXPORT_CHUNKS_PER_MIN = 2.0
# Size of one 1°x1° DTED cell (int16 posts + headers) below 50° latitude, by level.
DTED_CELL_MB = {0: 0.04, 1: 2.9, 2: 26.0}
JOB_ID_RE = re.compile(r"\d{8}-\d{6}-[0-9a-f]{6}")

# Plain-language explanation of each output mode: (short title, README paragraph lines).
MODE_TEXT = {
    "kongsberg": ("Kongsberg-ready (converted)", [
        "Every layer was merged into one seamless raster per source and reprojected to WGS84",
        "latitude/longitude (EPSG:4326), with overviews, ready to load into Kongsberg TerraLens."]),
    "clipped": ("Clipped, not converted", [
        "Each source file was cut to your area and otherwise left as it was: its own map projection,",
        "its own colours/palette and data type (elevation stays in metres). Nothing was merged or",
        "reprojected, so files from different sources do not line up pixel-for-pixel, and FAA chart",
        "borders and legends (collars) are still present. Each cut file is lossless unless its source",
        "was already JPEG-compressed imagery, and has internal overviews so it zooms out quickly.",
        "Use this with GIS tools that handle projections themselves; choose 'Kongsberg-ready' for",
        "TerraLens."]),
    "original": ("Original files, untouched", [
        "The source files exactly as their publishers distribute them: whole files, NOT cut to your",
        "area, so they usually cover much more ground than you selected, together with the files",
        "that belong with them (world files, metadata). Map services that don't publish files",
        "(tile/WMS/WMTS services) were cut to your area in the service's own projection instead.",
        "Use this for archiving, or to hand data to other GIS software unchanged."]),
}


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
            delivery.fail_interrupted(j)
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
        seen = set()
        for layer in layers:
            if layer["source"] not in reg:
                raise ValueError(f"Unknown source {layer['source']}")
            if layer["source"] in seen:
                raise ValueError(f"{reg[layer['source']].name} is selected twice")
            seen.add(layer["source"])
            if layer.get("res_m") not in (None, "") and not float(layer["res_m"]) > 0:
                raise ValueError(f"{reg[layer['source']].name}: resolution must be a positive number of metres")
        mode = spec.get("mode") or "kongsberg"
        if mode not in MODES:
            raise ValueError(f"Unknown output mode {mode!r} — choose one of: {', '.join(MODES)}")
        o = spec.get("outputs") or {}
        outputs = OutputOptions(geotiff=o.get("geotiff", True), cog=o.get("cog", False),
                                mbtiles=o.get("mbtiles", False),
                                dted_level=None if o.get("dted_level") in (None, "", "none") else int(o["dted_level"]),
                                overviews=o.get("overviews", True))
        # Formats only apply to Kongsberg-ready packages; the other modes keep each file's own format.
        if mode == "kongsberg" and not (outputs.geotiff or outputs.cog or outputs.mbtiles
                                        or outputs.dted_level is not None):
            raise ValueError("Choose at least one output format")
        return bbox, layers, outputs

    def estimate(self, spec: dict) -> dict:
        """Size and time of a job before running it: download (cache-aware, learned speeds)
        and the package each layer produces in the chosen output mode."""
        bbox, layers, outputs = self.validate(spec)
        mode = spec.get("mode") or "kongsberg"
        reg = registry(self.s)

        def source_estimate(layer):
            src = reg[layer["source"]]
            res = float(layer.get("res_m") or src.default_res_m)
            try:
                return src.estimate(bbox, res, self.s)
            except Exception as e:  # an estimate must never block building the job
                return {"download_mb": 0.0, "cached_pct": 0, "requests": None, "download_seconds": 0,
                        "speed_basis": "default", "notes": [f"Couldn't work out the download size ({e})."]}

        with ThreadPoolExecutor(max(1, min(8, len(layers)))) as pool:
            ests = list(pool.map(source_estimate, layers))

        rows = []
        for layer, est in zip(layers, ests):
            src = reg[layer["source"]]
            res = float(layer.get("res_m") or src.default_res_m)
            g = grid_for(bbox, res)
            bpp = 4 if src.kind == "elevation" else 3
            ratio = 0.08 if (src.kind == "rgb" and src.resampling != "nearest") else 0.3
            est_mb = round(g.pixels * bpp * ratio * 1.33 / 1e6, 1)
            row = {"source": src.id, "name": src.name, "res_m": res, "width": g.width, "height": g.height,
                   "megapixels": round(g.pixels / 1e6, 1), "est_mb": est_mb,
                   "too_big": g.pixels > self.s.max_pixels,
                   "download_mb": est["download_mb"], "cached_pct": est["cached_pct"],
                   "download_seconds": est["download_seconds"],
                   "package_mb": self._package_mb(mode, src, est, est_mb, outputs, bbox),
                   "speed_basis": est["speed_basis"], "notes": list(est["notes"])}
            if est.get("requests") is not None:
                row["requests"] = est["requests"]
            if hasattr(src, "chunk_count"):  # server-side exports (ArcGIS ImageServer) are slow per request
                row.setdefault("requests", src.chunk_count(bbox, res))
                row["fetch_minutes"] = round(est["download_seconds"] / 60)
            if row["too_big"] and mode == "kongsberg":
                row["notes"].append("Too big to build at this detail level — choose a coarser one or a smaller area.")
            rows.append(row)
        w, h = bbox.size_m()
        total_pkg = round(sum(r["package_mb"] for r in rows), 1)
        return {"area_km": [round(w / 1000, 1), round(h / 1000, 1)], "layers": rows,
                "total_download_mb": round(sum(r["download_mb"] for r in rows), 1),
                # Layers are processed one after another, so the times add up.
                "total_download_seconds": int(sum(r["download_seconds"] for r in rows)),
                "total_package_mb": total_pkg, "total_mb": total_pkg}

    @staticmethod
    def _package_mb(mode: str, src, est: dict, est_mb: float, outputs: OutputOptions, bbox: BBox) -> float:
        """Approximate size this layer adds to the package."""
        if mode == "original":
            v = est.get("source_mb", est.get("clipped_mb"))
            return round(v if v is not None else est_mb, 1)
        if mode == "clipped":
            v = est.get("clipped_mb")
            return round(v if v is not None else est_mb, 1)
        rasters = int(outputs.geotiff) + int(outputs.cog)
        if outputs.mbtiles and src.kind == "rgb":
            rasters += 1.1  # Web-Mercator tile pyramid, a bit bigger than one GeoTIFF
        mb = est_mb * rasters
        if src.kind == "elevation" and outputs.dted_level is not None:
            cells = ((math.ceil(bbox.east) - math.floor(bbox.west))
                     * (math.ceil(bbox.north) - math.floor(bbox.south)))
            mb += cells * DTED_CELL_MB.get(int(outputs.dted_level), 3.0)
        return round(mb, 1)

    # -- run ---------------------------------------------------------------------------
    def submit(self, spec: dict) -> dict:
        bbox, layers, outputs = self.validate(spec)
        mode = spec.get("mode") or "kongsberg"
        jid = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        name = _safe(spec.get("name") or "area")
        j = {"id": jid, "name": name, "created": time.time(), "status": "queued", "message": "Queued",
             "progress": 0.0, "spec": {**spec, "bbox": list(bbox.as_tuple()), "mode": mode}, "layers": [],
             "log": [], "package": None, "deliveries": []}
        with self.lock:
            self.jobs[jid] = j
            self.cancel[jid] = threading.Event()
        self._save(j)
        self.pool.submit(self._run, jid, bbox, layers, outputs, mode)
        return j

    def cancel_job(self, jid: str) -> None:
        if jid in self.cancel:
            self.cancel[jid].set()

    def delete(self, jid: str) -> bool:
        # Only ever remove directories of jobs we know about: jid comes from the URL.
        if jid not in self.jobs or not JOB_ID_RE.fullmatch(jid):
            return False
        self.cancel_job(jid)
        self.jobs.pop(jid, None)
        shutil.rmtree(self.s.jobs_dir / jid, ignore_errors=True)
        return True

    def _run(self, jid: str, bbox: BBox, layers: list[dict], outputs: OutputOptions,
             mode: str = "kongsberg") -> None:
        j = self.jobs[jid]
        pkg = self.s.jobs_dir / jid / j["name"]
        pkg.mkdir(parents=True, exist_ok=True)
        if self.cancel[jid].is_set():
            j.update(status="cancelled", message="Cancelled before start", finished=time.time())
            self._save(j)
            return
        j.update(status="running", package=str(pkg), started=time.time())
        last_save = [0.0]

        def progress(i: int, n: int):
            def cb(msg: str, frac: float | None = None):
                j["message"] = msg
                if frac is not None:
                    j["progress"] = round((i + frac) / n, 4)
                    j["layer_progress"] = round(frac, 4)
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
                j["current"] = {"index": i, "count": len(layers), "name": src.name}
                j["layer_progress"] = 0.0
                ctx = Context(self.s, progress(i, len(layers)), self.cancel[jid])
                lopts = LayerOptions(res_m=float(layer["res_m"]) if layer.get("res_m") else None,
                                     compression=layer.get("compression", "auto"))
                folder = f"{i + 1:02d}_{_safe(src.id)}"
                try:
                    r = build_layer(src, bbox, lopts, outputs, pkg / folder, ctx, _safe(src.id), mode=mode)
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
            if ok:  # last, so it covers the manifest and README too
                j["message"] = "Writing checksums (SHA256SUMS)…"
                delivery.write_checksums(pkg)
            j["status"] = "done" if ok and not failed else "partial" if ok else "failed"
            j["message"] = (f"{len(ok)} layer(s) ready" + (f", {len(failed)} failed" if failed else "")
                            if ok else "No layers produced — see layer messages")
        except Cancelled:
            j.update(status="cancelled", message="Cancelled")
        except Exception as e:
            j.update(status="failed", message=str(e))
        j["finished"] = time.time()
        j.pop("current", None)
        self._save(j)

    def _write_manifest(self, j: dict, pkg: Path, bbox: BBox) -> None:
        mode = j.get("spec", {}).get("mode") or "kongsberg"
        manifest = {"name": j["name"], "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(j["created"])),
                    "mode": mode, "mode_description": MODE_TEXT[mode][0],
                    "bbox_wsen": list(bbox.as_tuple()),
                    "crs": "EPSG:4326 (WGS84 geographic)" if mode == "kongsberg"
                    else "each file keeps its own projection (see layers[].file_info)",
                    "generator": "MapForge", "layers": j["layers"]}
        (pkg / "manifest.json").write_text(json.dumps(manifest, indent=2))
        if mode != "kongsberg":
            self._write_unconverted_readme(j, pkg, bbox, mode)
            return
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

    def _write_unconverted_readme(self, j: dict, pkg: Path, bbox: BBox, mode: str) -> None:
        """README for 'clipped' / 'original' packages (called from _write_manifest)."""
        title, body = MODE_TEXT[mode]
        lines = [f"MapForge package: {j['name']}",
                 f"Area (W,S,E,N): {', '.join(f'{v:.6f}' for v in bbox.as_tuple())}",
                 "", f"Mode: {title}", *body, "", "Layers and files:"]
        for x in j["layers"]:
            if x["status"] != "ok":
                lines.append(f"  (skipped) {x['name']}: {x.get('message', x['status'])}")
                continue
            lines += [f"  {x['folder']}/  {x['name']}  —  {x.get('summary', '')}",
                      f"      licence: {x.get('license', '')}"]
            for f in x.get("file_info", []):
                bits = [f.get("format", "")]
                if f.get("crs"):
                    bits.append(f"projection {f['crs']}")
                if f.get("width"):
                    bits.append(f"{f['width']}x{f['height']} px, {f['bands']} band(s) {f['dtype']}"
                                + (" (colour palette)" if f.get("palette") else ""))
                bits.append(f"{f['size_mb']:g} MB")
                lines.append(f"      {f['file']}: {'; '.join(b for b in bits if b)}")
            extra = [n for n in x.get("files", []) if n not in {f["file"] for f in x.get("file_info", [])}]
            if extra:
                lines.append(f"      alongside: {', '.join(extra[:8])}{' …' if len(extra) > 8 else ''}"
                             "  (world files / metadata that belong to the images above)")
        lines += ["",
                  "Handling: observe the licence / distribution statement of every source above. Aeronautical",
                  "charts are only current until their edition expires."]
        (pkg / "README.txt").write_text("\n".join(lines) + "\n")

    def zip_path(self, jid: str, layer: str | None = None) -> Path:
        j = self.jobs[jid]
        pkg = Path(j["package"])
        if layer is not None:  # one layer folder (+ README/manifest/SHA256SUMS)
            return delivery.layer_zip(pkg, layer, self.s.jobs_dir / jid / "layers", j.get("finished", 0))
        z = pkg.with_suffix(".zip")
        if not z.exists() or z.stat().st_mtime < j.get("finished", 0):
            tmp = z.with_suffix(".zip.part")
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED, allowZip64=True) as zf:
                for f in sorted(pkg.rglob("*")):
                    if f.is_file():
                        zf.write(f, f.relative_to(pkg.parent))
            tmp.replace(z)
        return z
