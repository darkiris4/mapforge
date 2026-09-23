"""Source plug-in interface.

A Source turns (bbox, resolution) into a list of Items: GDAL-openable rasters with a known
footprint.  The processing engine then warps and mosaics the items into the output grid, so
a new data provider only needs to know how to find/fetch rasters, never how to mosaic them.
"""
from __future__ import annotations

import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

import httpx

from ..geo import BBox
from ..settings import Settings

# Output kinds: "rgb" = 3-band 8-bit imagery/charts, "elevation" = 1-band float heights (m).
KINDS = ("rgb", "elevation")


class Cancelled(Exception):
    pass


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
            env["GDAL_HTTP_CAINFO"] = self.ca_bundle
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

    def download(self, url: str, dest: Path, auth: Auth | None = None, label: str = "") -> Path:
        """Download to dest (skipped when already cached), reporting progress."""
        if dest.exists() and dest.stat().st_size > 0:
            return dest
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".part")
        with self.http(auth, timeout=120) as c, c.stream("GET", url) as r:
            r.raise_for_status()
            total = int(r.headers.get("content-length") or 0)
            done = 0
            with open(tmp, "wb") as f:
                for chunk in r.iter_bytes(1 << 20):
                    self.check()
                    f.write(chunk)
                    done += len(chunk)
                    if total:
                        self.progress(f"Downloading {label or dest.name} ({done >> 20}/{total >> 20} MB)", None)
        tmp.replace(dest)
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
        }

    def has_coverage(self) -> bool:
        return False

    def coverage(self, ctx: Context) -> list[dict]:
        """Footprints for the map preview: [{label, bbox:[w,s,e,n]}]."""
        return []

    def items(self, bbox: BBox, res_m: float, ctx: Context) -> list[Item]:
        raise NotImplementedError
