"""Background job runner: one job = one area, several layers, one package."""
from __future__ import annotations

import json
import math
import multiprocessing
import re
import shutil
import threading
import time
import traceback
import uuid
import zipfile
from concurrent.futures import Future, ProcessPoolExecutor, ThreadPoolExecutor, wait
from pathlib import Path

from . import delivery
from .geo import BBox, bbox_of_points, grid_for, split_bbox
from .process import MODES, LayerOptions, OutputOptions, build_layer
from .settings import Settings
from .sources import Cancelled, Context, TooManyTiles, registry

# 96 dpi screen: one pixel ≈ 0.2646 mm, so a raster "looks native" at 1 : res_m / 0.0002646.
PX_M = 0.0002645833
# Observed throughput of USGS ImageServer exports (2000x2000 px, 4 in parallel): ~2 per minute.
# (Estimates use the learned rate from mapforge.speeds once a server has been measured.)
EXPORT_CHUNKS_PER_MIN = 2.0
# Size of one 1°x1° DTED cell (int16 posts + headers) below 50° latitude, by level.
DTED_CELL_MB = {0: 0.04, 1: 2.9, 2: 26.0}
JOB_ID_RE = re.compile(r"\d{8}-\d{6}-[0-9a-f]{6}")
# A too-big layer is split into a grid of tiles and each built independently (see _build_tiled):
# resilient (one tile's failure doesn't lose the rest), and each tile is a plain build_layer()
# call, so it's exactly as accurate as a normal single-area job. TILE_DEPTH bounds recursion if a
# tile is *still* too big after one split (rare — the first split is already sized to the actual
# overage) rather than splitting forever.
#
# Tiles build in separate OS PROCESSES, not threads. A tile's work has two very different phases
# — fetching (I/O-bound: threads are fine, the GIL is released during socket waits) and stitching
# the fetched pieces into one raster + cropping (CPU-bound: pure Python/rasterio work, which the
# GIL serializes to ~one core's worth of execution *no matter how many threads run it* — measured
# live on a real job: 5 threads doing this pinned the process at ~102% CPU, one tile's progress
# stalled for 10 minutes, and the whole HTTP API went unresponsive because the GIL-bound work
# starved the async server of scheduling turns). Separate processes each get their own GIL, so
# the CPU-bound phase actually uses multiple cores instead of just contending for one. Esri (and
# most anonymous public tile endpoints) publish no documented rate/concurrency limit — confirmed
# via research, not assumed — so TILE_WORKERS is a conservative, monitored value, not a number
# calibrated against an actual ceiling; raise it only with the same watch-for-429/503 approach.
MAX_TILE_DEPTH = 3
TILE_WORKERS = 5


def _tile_grid_size(factor: float) -> tuple[int, int]:
    """How many (cols, rows) tiles to split into for a request `factor` times over its limit —
    a roughly-square grid with at least `ceil(factor)` pieces."""
    n = max(2, math.ceil(factor))
    cols = max(1, math.ceil(math.sqrt(n)))
    rows = max(1, math.ceil(n / cols))
    return cols, rows


def _build_tile_worker(settings: Settings, source_id: str, bbox_tuple: tuple[float, float, float, float],
                       lopts: LayerOptions, outputs: OutputOptions, tile_dir: Path, safe_id: str, mode: str,
                       clip_polygon: list[tuple[float, float]] | None, progress_q, cancel_event,
                       depth: int, tile_index: int) -> dict:
    """Runs in its own OS process (see the TILE_WORKERS comment above for why). Only picklable
    values may cross a process boundary, so this rebuilds everything a normal Context needs from
    plain data instead of receiving one directly: the source is looked up fresh from the registry
    by id (sidesteps ever having to pickle a Source), and progress/cancellation go through a
    multiprocessing Queue/Event instead of the ordinary callback + threading.Event. Every progress
    message is tagged with ``tile_index`` so the parent can track each tile's own fraction and
    report a properly weighted overall progress, instead of several tiles racing to overwrite one
    shared number (the "progress bar keeps resetting" bug from running this as plain threads).
    """
    src = registry(settings)[source_id]
    bbox = BBox(*bbox_tuple)

    def progress(msg: str, frac: float | None = None) -> None:
        try:
            progress_q.put_nowait((tile_index, msg, frac))
        except Exception:
            pass

    class _RemoteCancel:
        def is_set(self) -> bool:
            return cancel_event.is_set()

        def wait(self, timeout: float) -> None:
            cancel_event.wait(timeout)

    ctx = Context(settings, progress, _RemoteCancel())
    try:
        return build_layer(src, bbox, lopts, outputs, tile_dir, ctx, safe_id, mode=mode,
                           clip_polygon=clip_polygon)
    except TooManyTiles as e:
        # A tile that's *still* too big after one split (rare — the first split is already sized
        # to the actual overage). Handled sequentially within this one process rather than
        # spinning up a nested process pool: deep recursion here is an edge case, not the common
        # path that needs real parallelism.
        if depth >= MAX_TILE_DEPTH:
            raise
        cols, rows = _tile_grid_size(e.factor)
        subs = split_bbox(bbox, cols, rows)
        results = []
        for i, sub in enumerate(subs):
            sub_dir = tile_dir / f"tile_{i:02d}"
            try:
                results.append(_build_tile_worker(settings, source_id, sub.as_tuple(), lopts, outputs, sub_dir,
                                                   safe_id, mode, clip_polygon, progress_q, cancel_event,
                                                   depth + 1, tile_index))
            except Cancelled:
                raise
            except Exception as ex:
                results.append({"status": "failed", "message": str(ex)})
        return JobManager._merge_tiles(src, bbox, mode, subs, results)


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
        self._save_lock = threading.Lock()
        self.jobs: dict[str, dict] = {}
        self.cancel: dict[str, threading.Event] = {}
        self._probe_lock = threading.Lock()
        self._probed: set[str] = set()  # source ids already background-probed this run
        self._mp_manager: multiprocessing.managers.SyncManager | None = None  # lazy: only tiled jobs need it
        self._mp_manager_lock = threading.Lock()
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
        # Progress can arrive from a heartbeat thread while the job thread also saves.
        with self._save_lock:
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
    def validate(self, spec: dict) -> tuple[BBox, list[dict], OutputOptions, list[tuple[float, float]] | None]:
        polygon = spec.get("polygon") or None
        if polygon is not None:
            if len(polygon) < 3:
                raise ValueError("A polygon needs at least 3 points")
            polygon = [(float(p[0]), float(p[1])) for p in polygon]
            if not all(math.isfinite(x) and math.isfinite(y) for x, y in polygon):
                raise ValueError("Polygon points must be finite numbers")
            if polygon[0] != polygon[-1]:  # close the ring: Item.clip / geometry_mask expect it
                polygon = [*polygon, polygon[0]]
            # Don't trust the client's bbox when a polygon is given — derive it fresh so the
            # fetch envelope and the clip shape can never drift apart.
            bbox = bbox_of_points(polygon)
            bbox.validate()
        else:
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
        return bbox, layers, outputs, polygon

    def _probe_in_background(self, src, bbox: BBox, res_m: float) -> None:
        """Kick a small real request off-thread to seed this run's actual speed for ``src``
        (see mapforge.speeds — a persisted rate from an earlier run/network isn't trusted as
        "measured" until this run has tested it). Fire-and-forget, at most once per source
        per run; the next estimate poll (the UI already re-polls on every change) picks up
        the result."""
        probe = getattr(src, "probe_speed", None)
        if not probe:
            return
        with self._probe_lock:
            if src.id in self._probed:
                return
            self._probed.add(src.id)

        def run():
            try:
                probe(bbox, res_m, Context(self.s))
            except Exception:
                pass  # best-effort background nicety, never surfaced to the user

        threading.Thread(target=run, name=f"probe-{src.id}", daemon=True).start()

    def estimate(self, spec: dict) -> dict:
        """Size and time of a job before running it: download (cache-aware, learned speeds)
        and the package each layer produces in the chosen output mode."""
        bbox, layers, outputs, _polygon = self.validate(spec)
        mode = spec.get("mode") or "kongsberg"
        reg = registry(self.s)

        def source_estimate(layer):
            src = reg[layer["source"]]
            res = float(layer.get("res_m") or src.default_res_m)
            try:
                est = src.estimate(bbox, res, self.s)
            except Exception as e:  # an estimate must never block building the job
                return {"download_mb": 0.0, "cached_pct": 0, "requests": None, "download_seconds": 0,
                        "speed_basis": "default", "notes": [f"Couldn't work out the download size ({e})."]}
            if est.get("speed_basis") == "default":
                self._probe_in_background(src, bbox, res)
            return est

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
                   # grid_for(bbox, res) is the *Kongsberg mosaic* output grid — meaningless for
                   # "clipped"/"original" modes, which never resample to it (they copy/crop each
                   # source file at its own native resolution). Flagging it there blocked jobs
                   # for a reason that didn't apply to what those modes actually do.
                   "too_big": mode == "kongsberg" and g.pixels > self.s.max_pixels,
                   "download_mb": est["download_mb"], "cached_pct": est["cached_pct"],
                   "download_seconds": est["download_seconds"],
                   "package_mb": self._package_mb(mode, src, est, est_mb, outputs, bbox),
                   "speed_basis": est["speed_basis"], "notes": list(est["notes"])}
            if est.get("requests") is not None:
                row["requests"] = est["requests"]
            if hasattr(src, "chunk_count"):  # server-side exports (ArcGIS ImageServer) are slow per request
                row.setdefault("requests", src.chunk_count(bbox, res))
                row["fetch_minutes"] = round(est["download_seconds"] / 60)
            if row["too_big"]:
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
        bbox, layers, outputs, polygon = self.validate(spec)
        mode = spec.get("mode") or "kongsberg"
        jid = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        name = _safe(spec.get("name") or "area")
        stored_spec = {**spec, "bbox": list(bbox.as_tuple()), "mode": mode}
        if polygon is not None:
            stored_spec["polygon"] = [list(p) for p in polygon]
        j = {"id": jid, "name": name, "created": time.time(), "status": "queued", "message": "Queued",
             "progress": 0.0, "spec": stored_spec, "layers": [],
             "log": [], "package": None, "deliveries": []}
        with self.lock:
            self.jobs[jid] = j
            self.cancel[jid] = threading.Event()
        self._save(j)
        self.pool.submit(self._run, jid, bbox, layers, outputs, mode, polygon)
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

    # -- tiling: split a too-big layer into pieces instead of failing the whole job -----------
    def _manager(self) -> multiprocessing.managers.SyncManager:
        """One shared manager process for the server's lifetime — lazily started, since most jobs
        never tile and don't need it."""
        if self._mp_manager is None:
            with self._mp_manager_lock:
                if self._mp_manager is None:
                    self._mp_manager = multiprocessing.Manager()
        return self._mp_manager

    def _build_tiled(self, src, bbox: BBox, lopts: LayerOptions, outputs: OutputOptions, out_dir: Path,
                     ctx: Context, safe_id: str, mode: str,
                     clip_polygon: list[tuple[float, float]] | None) -> dict:
        try:
            return build_layer(src, bbox, lopts, outputs, out_dir, ctx, safe_id, mode=mode,
                               clip_polygon=clip_polygon)
        except TooManyTiles as e:
            cols, rows = _tile_grid_size(e.factor)
            subs = split_bbox(bbox, cols, rows)
            ctx.progress(f"{src.name}: too much for one piece — splitting into {len(subs)} tiles "
                        f"({cols}x{rows}), building across separate processes for real multi-core speed", 0.0)

            # A tile whose output already finished in a previous attempt at this same job is
            # reused without spinning up a process for it at all.
            results: list[dict | None] = [None] * len(subs)
            todo: list[tuple[int, BBox]] = []
            for i, sub in enumerate(subs):
                marker = out_dir / f"tile_{i:02d}" / "layer.json"
                if marker.exists():
                    try:
                        cached = json.loads(marker.read_text())
                        if cached.get("status") in ("ok", "empty"):
                            results[i] = cached
                            continue
                    except ValueError:
                        pass
                todo.append((i, sub))
            if not todo:
                return self._merge_tiles(src, bbox, mode, subs, results)

            manager = self._manager()
            progress_q = manager.Queue()
            cancel_event = manager.Event()
            if ctx.cancel_event.is_set():
                cancel_event.set()
            frac_by_tile: dict[int, float] = {}
            stop_draining = threading.Event()

            def drain() -> None:
                while True:
                    try:
                        i, msg, frac = progress_q.get(timeout=0.5)
                    except Exception:
                        if stop_draining.is_set():
                            return
                        continue
                    if frac is not None:
                        frac_by_tile[i] = frac
                    # Weighted across ALL tiles (not just the ones that have reported yet), so
                    # this is a proper monotonic-ish overall fraction instead of whichever tile's
                    # update happened to land last (the old bug, running these as plain threads).
                    ctx.progress(msg, sum(frac_by_tile.get(k, 0.0) for k in range(len(subs))) / len(subs))

            drain_thread = threading.Thread(target=drain, daemon=True)
            drain_thread.start()
            try:
                with ProcessPoolExecutor(min(TILE_WORKERS, len(todo))) as pool:
                    futs: dict[Future, int] = {}
                    for i, sub in todo:
                        tile_dir = out_dir / f"tile_{i:02d}"
                        fut = pool.submit(_build_tile_worker, self.s, src.id, sub.as_tuple(), lopts, outputs,
                                          tile_dir, safe_id, mode, clip_polygon, progress_q, cancel_event, 0, i)
                        futs[fut] = i
                    pending = set(futs)
                    while pending:
                        if ctx.cancel_event.is_set():
                            cancel_event.set()
                        done, pending = wait(pending, timeout=1.0)
                        for fut in done:
                            i = futs[fut]
                            try:
                                results[i] = fut.result()
                            except Exception as ex:
                                results[i] = {"status": "failed", "message": str(ex)}
                    if ctx.cancel_event.is_set():
                        raise Cancelled()
            finally:
                stop_draining.set()
                drain_thread.join(timeout=2)
            return self._merge_tiles(src, bbox, mode, subs, results)

    @staticmethod
    def _merge_tiles(src, bbox: BBox, mode: str, subs: list[BBox], results: list[dict]) -> dict:
        """Combine independent per-tile build_layer() results into one layer entry — same shape
        _run()/the manifest/README already expect, so nothing downstream needs to know a layer
        was tiled at all beyond the added ``tiles`` list."""
        tiles_meta = [{"bbox": list(sub.as_tuple()), "folder": f"tile_{i:02d}",
                      "status": (results[i] or {}).get("status", "failed")} for i, sub in enumerate(subs)]
        ok = [r for r in results if r and r.get("status") == "ok"]
        n_ok, n = len(ok), len(results)
        if not ok:
            failed = next((r for r in results if r and r.get("status") == "failed"), None)
            return {"source": src.id, "name": src.name, "license": src.license, "mode": mode,
                   "bbox": list(bbox.as_tuple()), "tiles": tiles_meta,
                   "status": "failed" if failed else "empty",
                   "message": failed["message"] if failed else "No data from this source intersects the area."}
        files = [f"tile_{i:02d}/{f}" for i, r in enumerate(results) if r and r.get("status") == "ok"
                for f in r.get("files", [])]
        file_info = [{**fi, "file": f"tile_{i:02d}/{fi['file']}"} for i, r in enumerate(results)
                    if r and r.get("status") == "ok" for fi in r.get("file_info", [])]
        first = ok[0]
        summary = f"{n_ok} of {n} tile{'s' if n != 1 else ''} built" + (f", {n - n_ok} failed" if n_ok < n else "")
        return {"source": src.id, "name": src.name, "kind": first.get("kind", src.kind), "license": src.license,
               "mode": mode, "res_m": first.get("res_m"), "bbox": list(bbox.as_tuple()),
               "crs": first.get("crs"), "files": files, "file_info": file_info,
               "size_mb": round(sum(r.get("size_mb", r.get("est_mb", 0)) for r in ok), 2),
               "summary": summary + (f" — {first.get('summary')}" if first.get("summary") else ""),
               "status": "ok", "tiles": tiles_meta}

    def _run(self, jid: str, bbox: BBox, layers: list[dict], outputs: OutputOptions,
             mode: str = "kongsberg", clip_polygon: list[tuple[float, float]] | None = None) -> None:
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
                j["updated"] = time.time()  # "last activity" for the UI, even when the bar can't move
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
                    r = self._build_tiled(src, bbox, lopts, outputs, pkg / folder, ctx, _safe(src.id), mode,
                                          clip_polygon)
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
            # A too-big layer was split into tiles (see _build_tiled/_merge_tiles) — it has no
            # single width/height, just a list of independently-sized tile pieces.
            size_line = (f"      {x['res_m']} m/px, split into {len(x['tiles'])} tile(s) — {x.get('summary', '')}"
                        if x.get("tiles") else
                        f"      {x['res_m']} m/px, {x['width']}x{x['height']} px, coverage {x.get('coverage_pct', '?')}%")
            lines += [f"  {x['folder']}/  {x['name']}", size_line,
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
