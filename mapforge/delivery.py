"""Getting a finished package to where it's needed.

- SHA256SUMS in every package (``sha256sum -c`` compatible) so a copy can be proven intact.
- Per-layer zips, for when only one layer has to move.
- Export: copy a package into an allow-listed folder on this server (e.g. a mounted share).
- Split: cut the package zip into fixed-size parts for removable media, with join instructions.

Exports and splits run in background threads; their state lives in ``job["deliveries"]`` so the
UI can poll it like job progress.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import threading
import time
import uuid
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .jobs import JobManager

SUMS = "SHA256SUMS"
JOIN_README = "JOIN-README.txt"
MIN_PART_MB = 50
SAFE_FILE = re.compile(r"(?!\.{1,2}$)[A-Za-z0-9._-]+")  # a bare file/folder name, never . or ..
_CHUNK = 4 << 20


class DeliveryError(ValueError):
    """A request that can't be honoured (bad path, bad size, …) — maps to HTTP 400."""


class DeliveryConflict(DeliveryError):
    """Something already exists or is already running — maps to HTTP 409."""


# --------------------------------------------------------------------------------------
# Checksums
# --------------------------------------------------------------------------------------
def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def _package_files(pkg: Path) -> list[Path]:
    return sorted(p for p in pkg.rglob("*") if p.is_file() and p.name != SUMS)


def write_checksums(pkg: Path) -> Path:
    """Write <pkg>/SHA256SUMS covering every file in the package (paths relative, '/' separated)."""
    lines = [f"{sha256_file(p)}  {p.relative_to(pkg).as_posix()}" for p in _package_files(pkg)]
    out = pkg / SUMS
    tmp = out.with_name(SUMS + ".part")
    tmp.write_text("\n".join(lines) + "\n")
    tmp.replace(out)
    return out


def verify_checksums(folder: Path) -> tuple[int, list[str]]:
    """Check files listed in folder/SHA256SUMS. Returns (number OK, list of problems)."""
    sums = folder / SUMS
    if not sums.exists():
        return 0, [f"{SUMS} missing"]
    ok, bad = 0, []
    for line in sums.read_text().splitlines():
        if not line.strip():
            continue
        digest, _, rel = line.partition("  ")
        p = folder / rel
        if not p.is_file():
            bad.append(f"{rel}: missing")
        elif sha256_file(p) != digest:
            bad.append(f"{rel}: checksum mismatch")
        else:
            ok += 1
    return ok, bad


# --------------------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------------------
def export_roots(settings) -> list[Path]:
    return [Path(os.path.realpath(r)) for r in settings.export_dirs]


def resolve_dest(settings, dest: str) -> Path:
    """Resolve a user-supplied destination and prove it lies under an allowed export root.

    realpath() resolves every existing symlink on the way, so a link inside a root that points
    elsewhere is judged by where it really goes.
    """
    if not dest or not str(dest).strip():
        raise DeliveryError("Choose a destination folder")
    raw = Path(str(dest).strip()).expanduser()
    if not raw.is_absolute():
        raise DeliveryError("The destination must be an absolute path (e.g. /mnt/share/maps)")
    real = Path(os.path.realpath(raw))
    for root in export_roots(settings):
        if real == root or real.is_relative_to(root):
            return real
    roots = ", ".join(str(r) for r in export_roots(settings))
    raise DeliveryError(f"{raw} is outside the allowed export folders ({roots}). "
                        "An administrator can allow more with MAPFORGE_EXPORT_DIRS.")


def layer_folder(pkg: Path, folder: str) -> Path:
    """Validate a layer folder name taken from a URL."""
    if not folder or not SAFE_FILE.fullmatch(folder):
        raise DeliveryError("Bad layer name")
    p = pkg / folder
    if not p.is_dir() or p.is_symlink() or p.parent != pkg:
        raise DeliveryError(f"No layer '{folder}' in this package")
    return p


# --------------------------------------------------------------------------------------
# Zips
# --------------------------------------------------------------------------------------
def _zip_members(zf: zipfile.ZipFile, files: list[Path], base: Path) -> None:
    for f in files:
        zf.write(f, f.relative_to(base).as_posix())


def build_zip(dest: Path, files: list[Path], base: Path) -> Path:
    tmp = dest.with_name(dest.name + ".part")
    dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED, allowZip64=True) as zf:
        _zip_members(zf, files, base)
    tmp.replace(dest)
    return dest


def layer_zip(pkg: Path, folder: str, out_dir: Path, not_before: float = 0) -> Path:
    """Zip one layer folder plus the package README / manifest / SHA256SUMS."""
    lf = layer_folder(pkg, folder)
    z = out_dir / f"{pkg.name}_{folder}.zip"
    if z.exists() and z.stat().st_mtime >= not_before:
        return z
    files = sorted(p for p in lf.rglob("*") if p.is_file())
    files += [pkg / n for n in ("README.txt", "manifest.json", SUMS) if (pkg / n).is_file()]
    return build_zip(z, files, pkg.parent)


# --------------------------------------------------------------------------------------
# Background deliveries
# --------------------------------------------------------------------------------------
def _new_delivery(jm: "JobManager", j: dict, kind: str, **extra) -> dict:
    d = {"id": uuid.uuid4().hex[:8], "type": kind, "status": "running", "message": "Starting…",
         "path": None, "files": [], "created": time.time(), **extra}
    with jm.lock:
        j.setdefault("deliveries", []).append(d)
    jm._save(j)
    return d


def _finish(jm: "JobManager", j: dict, d: dict, status: str, message: str) -> None:
    d.update(status=status, message=message, finished=time.time())
    jm._save(j)


def _require_ready(j: dict | None) -> dict:
    if not j or j.get("status") not in ("done", "partial") or not j.get("package"):
        raise DeliveryError("The package isn't ready yet")
    return j


def _running(j: dict, kind: str) -> bool:
    return any(d["type"] == kind and d["status"] == "running" for d in j.get("deliveries", []))


def start_export(jm: "JobManager", jid: str, dest: str, overwrite: bool = False) -> dict:
    j = _require_ready(jm.get(jid))
    pkg = Path(j["package"])
    base = resolve_dest(jm.s, dest)
    target = base / pkg.name
    # Re-check after joining the name: a pre-existing symlink named like the package must not escape.
    resolve_dest(jm.s, str(target))
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        if not overwrite:
            raise DeliveryConflict(f"{target} already exists and isn't empty. Tick 'overwrite' to replace it.")
    if _running(j, "export"):
        raise DeliveryConflict("An export of this package is already running")
    d = _new_delivery(jm, j, "export", path=str(target))
    threading.Thread(target=_do_export, args=(jm, j, d, pkg, base, target, overwrite), daemon=True,
                     name=f"export-{jid}").start()
    return d


def _do_export(jm, j, d, pkg: Path, base: Path, target: Path, overwrite: bool) -> None:
    try:
        files = _package_files(pkg) + ([pkg / SUMS] if (pkg / SUMS).exists() else [])
        total = sum(f.stat().st_size for f in files) or 1
        base.mkdir(parents=True, exist_ok=True)
        tmp = base / f".{target.name}.mapforge-part-{d['id']}"
        shutil.rmtree(tmp, ignore_errors=True)
        done, last = 0, 0.0
        for f in files:
            out = tmp / f.relative_to(pkg)
            out.parent.mkdir(parents=True, exist_ok=True)
            with open(f, "rb") as src, open(out, "wb") as dst:
                for chunk in iter(lambda: src.read(_CHUNK), b""):
                    dst.write(chunk)
                    done += len(chunk)
                    if time.time() - last > 1:
                        last = time.time()
                        d["message"] = f"Copying… {done / 1e6:,.0f} of {total / 1e6:,.0f} MB ({100 * done / total:.0f}%)"
                        jm._save(j)
            shutil.copystat(f, out)
        d["message"] = "Verifying copy (SHA-256)…"
        jm._save(j)
        ok, bad = verify_checksums(tmp)
        if bad:
            raise RuntimeError(f"Copy failed verification: {'; '.join(bad[:5])}")
        if target.exists():
            if not overwrite:
                raise RuntimeError(f"{target} appeared while copying; not overwriting it")
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            else:
                target.unlink()
        tmp.rename(target)
        d["files"] = [p.relative_to(target).as_posix() for p in sorted(target.rglob("*")) if p.is_file()]
        _finish(jm, j, d, "done", f"Copied {len(d['files'])} files ({total / 1e6:,.1f} MB) to {target}; "
                                  f"all {ok} checksums verified")
    except Exception as e:  # noqa: BLE001 — report to the UI
        shutil.rmtree(base / f".{target.name}.mapforge-part-{d['id']}", ignore_errors=True)
        _finish(jm, j, d, "failed", f"Export failed: {e}")


def parts_dir(jm: "JobManager", jid: str) -> Path:
    return jm.s.jobs_dir / jid / "parts"


def start_split(jm: "JobManager", jid: str, part_mb: int) -> dict:
    j = _require_ready(jm.get(jid))
    try:
        part_mb = int(part_mb)
    except (TypeError, ValueError):
        raise DeliveryError("Part size must be a whole number of MB")
    if part_mb < MIN_PART_MB:
        raise DeliveryError(f"Parts must be at least {MIN_PART_MB} MB")
    if _running(j, "split"):
        raise DeliveryConflict("A split of this package is already running")
    out = parts_dir(jm, jid)
    for prev in j.get("deliveries", []):
        if (prev["type"] == "split" and prev["status"] == "done" and prev.get("part_mb") == part_mb
                and prev.get("files") and all((out / f).is_file() for f in prev["files"])):
            return prev  # identical split already on disk
    d = _new_delivery(jm, j, "split", part_mb=part_mb, path=str(out))
    threading.Thread(target=_do_split, args=(jm, jid, j, d, part_mb, out), daemon=True,
                     name=f"split-{jid}").start()
    return d


def _do_split(jm, jid: str, j: dict, d: dict, part_mb: int, out: Path) -> None:
    try:
        d["message"] = "Building the package zip…"
        jm._save(j)
        z = jm.zip_path(jid)
        # A new split replaces any earlier one (different size) for this job.
        shutil.rmtree(out, ignore_errors=True)
        with jm.lock:  # its files are gone, so drop the superseded entry
            j["deliveries"] = [p for p in j.get("deliveries", [])
                               if p is d or not (p["type"] == "split" and p["status"] != "running")]
        out.mkdir(parents=True)
        size = z.stat().st_size
        part_bytes = part_mb << 20
        n_parts = max(1, -(-size // part_bytes))
        width = max(3, len(str(n_parts)))
        whole = hashlib.sha256()
        lines, files = [], []
        with open(z, "rb") as src:
            for i in range(1, n_parts + 1):
                name = f"{z.name}.{i:0{width}d}"
                h = hashlib.sha256()
                left = part_bytes
                with open(out / name, "wb") as dst:
                    while left > 0:
                        chunk = src.read(min(_CHUNK, left))
                        if not chunk:
                            break
                        dst.write(chunk)
                        h.update(chunk)
                        whole.update(chunk)
                        left -= len(chunk)
                lines.append(f"{h.hexdigest()}  {name}")
                files.append(name)
                d["message"] = f"Writing part {i} of {n_parts}…"
                jm._save(j)
        lines.append(f"{whole.hexdigest()}  {z.name}")
        (out / SUMS).write_text("\n".join(lines) + "\n")
        (out / JOIN_README).write_text(_join_readme(z.name, files, part_mb, size))
        d["files"] = files + [SUMS, JOIN_README]
        _finish(jm, j, d, "done", f"{n_parts} part(s) of up to {part_mb} MB ({size / 1e6:,.1f} MB total)")
    except Exception as e:  # noqa: BLE001
        _finish(jm, j, d, "failed", f"Split failed: {e}")


def _join_readme(zip_name: str, parts: list[str], part_mb: int, size: int) -> str:
    first, plus = parts[0], "+".join(parts)
    return f"""MapForge package split into {len(parts)} part(s) of up to {part_mb} MB
Joined file: {zip_name} ({size:,} bytes)

Copy ALL of these files into one folder, then join them:

Linux / macOS
  sha256sum -c --ignore-missing {SUMS}     # optional: check the parts first
  cat {zip_name}.* > {zip_name}
  sha256sum -c {SUMS}                      # checks the parts AND the joined zip
  unzip {zip_name}

Windows (Command Prompt)
  copy /b {plus} {zip_name}
  certutil -hashfile {zip_name} SHA256     (compare with the {zip_name} line in {SUMS})

Windows / Linux with 7-Zip
  Open {first} in 7-Zip — it reads the remaining parts automatically; extract as usual.

The {SUMS} file lists one SHA-256 per part plus the joined {zip_name}.
Inside the zip, the package has its own {SUMS}: after unzipping, run `sha256sum -c {SUMS}`
in the package folder to prove every map file arrived intact.
"""


def fail_interrupted(j: dict) -> bool:
    """On startup: deliveries that were running when the server stopped did not finish."""
    changed = False
    for d in j.get("deliveries", []):
        if d.get("status") == "running":
            d.update(status="failed", message="Interrupted by a server restart — start it again.")
            changed = True
    return changed
