"""Source plug-in interface.

A Source turns (bbox, resolution) into a list of Items: GDAL-openable rasters with a known
footprint.  The processing engine then warps and mosaics the items into the output grid, so
a new data provider only needs to know how to find/fetch rasters, never how to mosaic them.
"""
from __future__ import annotations

import math
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

import httpx

from .. import speeds
from ..geo import BBox
from ..settings import Settings

# Output kinds: "rgb" = 3-band 8-bit imagery/charts, "elevation" = 1-band float heights (m).
KINDS = ("rgb", "elevation")


class Cancelled(Exception):
    pass


_dl_locks: dict[str, threading.Lock] = {}
_dl_guard = threading.Lock()


def _download_lock(dest: Path) -> threading.Lock:
    with _dl_guard:
        return _dl_locks.setdefault(str(dest), threading.Lock())


@dataclass
class Auth:
    """Credentials for a protected endpoint (e.g. an NGA/GEGD service behind PKI).

    pki    -> client certificate + key in PEM form (a soft cert, or one exported from a
              token that allows it).  CAC private keys normally cannot be exported; for those
              use a PKCS#11-aware proxy (see README) and point the endpoint at the proxy.
    basic  -> username/password.
    header -> an arbitrary header, e.g. "Authorization: Bearer ...".
    """

    type: str = "none"
    cert: str | None = None
    key: str | None = None
    key_password: str | None = None
    ca_bundle: str | None = None
    username: str | None = None
    password: str | None = None
    header: str | None = None

    @classmethod
    def from_dict(cls, d: dict | None) -> "Auth":
        if not d:
            return cls()
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__ and v not in ("", None)})

    def public(self) -> dict:
        d = asdict(self)
        for k in ("key_password", "password", "header"):
            if d.get(k):
                d[k] = "********"
        return d

    def gdal_env(self) -> dict[str, str]:
        env: dict[str, str] = {}
        if self.ca_bundle:
            # rasterio wheels export GDAL_CURL_CA_BUNDLE (certifi) at import, which takes
            # precedence over GDAL_HTTP_CAINFO — override every spelling so a private/DoD CA is used.
            env["GDAL_HTTP_CAINFO"] = self.ca_bundle
            env["GDAL_CURL_CA_BUNDLE"] = self.ca_bundle
            env["CURL_CA_BUNDLE"] = self.ca_bundle
        if self.type == "pki":
            if self.cert:
                env["GDAL_HTTP_SSLCERT"] = self.cert
                env["GDAL_HTTP_SSLCERTTYPE"] = "PEM"
            if self.key:
                env["GDAL_HTTP_SSLKEY"] = self.key
            if self.key_password:
                env["GDAL_HTTP_KEYPASSWD"] = self.key_password
        elif self.type == "basic" and self.username:
            env["GDAL_HTTP_AUTH"] = "BASIC"
            env["GDAL_HTTP_USERPWD"] = f"{self.username}:{self.password or ''}"
        elif self.type == "header" and self.header:
            env["GDAL_HTTP_HEADERS"] = self.header
        return env

    def httpx_kwargs(self) -> dict:
        kw: dict = {}
        if self.ca_bundle:
            kw["verify"] = self.ca_bundle
        if self.type == "pki" and self.cert:
            import ssl

            ctx = ssl.create_default_context(cafile=self.ca_bundle) if self.ca_bundle else ssl.create_default_context()
            ctx.load_cert_chain(self.cert, self.key, self.key_password)
            kw["verify"] = ctx
        elif self.type == "basic" and self.username:
            kw["auth"] = (self.username, self.password or "")
        elif self.type == "header" and self.header and ":" in self.header:
            k, v = self.header.split(":", 1)
            kw["headers"] = {k.strip(): v.strip()}
        return kw


@dataclass
class Item:
    """One GDAL-openable raster that contributes to a layer."""

    path: str  # anything rasterio.open accepts (file, /vsizip/..., /vsicurl/..., WMS XML)
    footprint: BBox  # lon/lat extent used for selection and mosaic priority
    label: str = ""
    gdal_env: dict[str, str] = field(default_factory=dict)
    native_res_m: float | None = None
    # Optional lon/lat polygons ([(lon, lat), ...]); only pixels inside all of them are used.
    # Charts use this to drop collars/legends outside the neatline.
    clip: list[list[tuple[float, float]]] | None = None
    # True for rasters built from a map service (tiles/WMS), which publishes no files to copy.
    service: bool = False


@dataclass
class Context:
    settings: Settings
    progress: Callable[[str, float | None], None] = lambda msg, frac=None: None
    cancel_event: threading.Event = field(default_factory=threading.Event)

    def check(self) -> None:
        if self.cancel_event.is_set():
            raise Cancelled()

    def http(self, auth: Auth | None = None, timeout: float = 60) -> httpx.Client:
        kw = (auth or Auth()).httpx_kwargs()
        headers = {"User-Agent": self.settings.user_agent, **kw.pop("headers", {})}
        return httpx.Client(timeout=timeout, follow_redirects=True, headers=headers, **kw)

    def download(self, url: str, dest: Path, auth: Auth | None = None, label: str = "",
                 attempts: int = 6) -> Path:
        """Download to dest (skipped when already cached), reporting progress.

        Stalls and dropped connections are retried with HTTP Range resume, and concurrent
        jobs fetching the same file wait for one another instead of clobbering the .part file.
        """
        with _download_lock(dest):
            if dest.exists() and dest.stat().st_size > 0:
                return dest
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(dest.name + ".part")
            name = label or dest.name
            timeout = httpx.Timeout(30.0, read=60.0)
            t_start, fetched = time.monotonic(), 0  # for the learned-speed sample
            for attempt in range(attempts):
                self.check()
                have = tmp.stat().st_size if tmp.exists() else 0
                headers = {"Range": f"bytes={have}-"} if have else {}
                try:
                    with self.http(auth, timeout=timeout) as c, c.stream("GET", url, headers=headers) as r:
                        if r.status_code == 416:  # already complete
                            break
                        r.raise_for_status()
                        if have and r.status_code != 206:  # server ignored Range: start over
                            have = 0
                        total = have + int(r.headers.get("content-length") or 0)
                        done, t0, last = have, time.monotonic(), 0.0
                        with open(tmp, "ab" if have else "wb") as f:
                            for chunk in r.iter_bytes(1 << 20):
                                self.check()
                                f.write(chunk)
                                done += len(chunk)
                                fetched += len(chunk)
                                now = time.monotonic()
                                if now - last > 1:
                                    last = now
                                    rate = (done - have) / max(now - t0, 1e-3) / 1e6
                                    size = f"{done / 1e6:.0f}/{total / 1e6:.0f} MB" if total > have else f"{done / 1e6:.0f} MB"
                                    self.progress(f"Downloading {name} ({size}, {rate:.1f} MB/s)", None)
                    if total <= have or tmp.stat().st_size >= total:
                        break
                except (httpx.TransportError, httpx.RemoteProtocolError) as e:
                    if attempt == attempts - 1:
                        raise RuntimeError(f"Download of {name} failed after {attempts} attempts: {e}") from e
                    self.progress(f"Downloading {name}: connection problem ({type(e).__name__}), resuming…", None)
                    time.sleep(min(30, 2 * 2**attempt))
            tmp.replace(dest)
            if fetched >= 1 << 20:  # ignore tiny files: latency dominates
                speeds.record(self.settings, speeds.host_key("bytes", url), fetched, time.monotonic() - t_start)
            return dest


class Source:
    id: str = ""
    name: str = ""
    group: str = ""
    kind: str = "rgb"
    access: str = "public"  # public | pki | local
    description: str = ""
    license: str = ""
    default_res_m: float = 30.0
    min_res_m: float = 0.1  # finest resolution worth requesting
    # "nearest" keeps chart linework crisp; imagery/elevation look better bilinear.
    resampling: str = "bilinear"
    # Plain-language metadata for people who are not GIS specialists (guided mode).
    category: str = "custom"  # vfr | ifr | imagery | elevation | local | custom
    plain_name: str = ""
    explain: str = ""

    def info(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "group": self.group,
            "kind": self.kind,
            "access": self.access,
            "description": self.description,
            "license": self.license,
            "default_res_m": self.default_res_m,
            "min_res_m": self.min_res_m,
            "coverage": self.has_coverage(),
            "category": self.category,
            "plain_name": self.plain_name or self.name,
            "explain": self.explain or self.description,
            "detail_levels": self.detail_levels(),
        }

    def detail_levels(self) -> list[dict]:
        """Resolution choices in plain words, ordered coarse -> fine."""
        return generic_detail_levels(self.kind, self.default_res_m, self.min_res_m)

    def estimate(self, bbox: BBox, res_m: float, settings: Settings) -> dict:
        """What fetching this layer will cost, before anything is downloaded.

        Returns download_mb (still to fetch; cache excluded), cached_pct, requests,
        download_seconds, notes (plain sentences), speed_basis ("measured"|"default"),
        and optionally source_mb (full source files, for "original" packages) and
        clipped_mb (source cut to the box, for "clipped" packages).
        Sources that cannot say anything useful return an unknown-cost estimate.
        """
        return estimate_result(notes=["Download size can't be predicted for this source."])

    def has_coverage(self) -> bool:
        return False

    def coverage(self, ctx: Context) -> list[dict]:
        """Footprints for the map preview: [{label, bbox:[w,s,e,n]}]."""
        return []

    def items(self, bbox: BBox, res_m: float, ctx: Context) -> list[Item]:
        raise NotImplementedError


# --------------------------------------------------------------------------------------
# Estimate helpers shared by the sources
# --------------------------------------------------------------------------------------
def estimate_result(download_bytes: float = 0.0, cached_pct: float = 0.0, requests: int | None = None,
                    seconds: float = 0.0, notes: list[str] | None = None, basis: str = "default",
                    source_bytes: float | None = None, clipped_bytes: float | None = None) -> dict:
    out = {"download_mb": round(download_bytes / 1e6, 1), "cached_pct": round(max(0.0, min(100.0, cached_pct))),
           "requests": requests, "download_seconds": int(math.ceil(seconds)), "notes": notes or [],
           "speed_basis": basis}
    if source_bytes is not None:
        out["source_mb"] = round(source_bytes / 1e6, 1)
    if clipped_bytes is not None:
        out["clipped_mb"] = round(clipped_bytes / 1e6, 1)
    return out


def plain_duration(seconds: float) -> str:
    s = max(0, int(round(seconds)))
    if s < 60:
        return "under a minute"
    if s < 3600:
        return f"about {max(1, round(s / 60))} min"
    return f"about {s / 3600:.1f} h"


def speed_note(basis: str) -> list[str]:
    return [] if basis == "measured" else [
        "Time is a rough guess until MapForge has measured this server's speed on your network."]


def cached_note(cached_pct: float, download_bytes: float) -> list[str]:
    if download_bytes <= 0 and cached_pct >= 99.5:
        return ["Already downloaded earlier — no wait."]
    if cached_pct >= 1:
        return [f"About {round(cached_pct)}% is already downloaded; only the rest is fetched."]
    return []


def _level(id_: str, label: str, res_m: float, hint: str) -> dict:
    return {"id": id_, "label": label, "res_m": round(float(res_m), 3), "hint": hint}


def generic_detail_levels(kind: str, default_res_m: float, min_res_m: float = 0.1) -> list[dict]:
    """Sensible coarse->fine choices around a source's normal resolution."""
    d = float(default_res_m or 10.0)
    if kind == "elevation":
        levels = [_level("overview", "Coarse terrain", d * 4, "Broad hills and valleys; small download"),
                  _level("native", "Full detail", d, "The source's own spacing")]
    else:
        levels = [_level("overview", "Wide-area overview", d * 8, "Coastlines, cities and major roads"),
                  _level("area", "Area detail", d * 3, "Streets and large buildings"),
                  _level("native", "Full detail", d, "The source's own resolution")]
    return [lv for lv in levels if lv["res_m"] >= (min_res_m or 0) * 0.999]
