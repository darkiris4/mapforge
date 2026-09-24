"""Localhost mutual-TLS map server for offline PKI tests.

Generates a throwaway CA, a server certificate for localhost/127.0.0.1 and a client
certificate whose key is password protected, then serves:

  /tiles/{z}/{x}/{y}.png            XYZ tiles (solid colour)
  /wms?...&BBOX=..&WIDTH=..         WMS 1.1.1 GetMap (solid colour PNG)
  /arcgis/ImageServer/exportImage   ArcGIS exportImage (georeferenced GeoTIFF)

Requests without a client certificate signed by the CA fail the TLS handshake.
"""
from __future__ import annotations

import shutil
import ssl
import subprocess
import sys
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import numpy as np
import rasterio
from rasterio.io import MemoryFile
from rasterio.transform import from_bounds

COLOR = (200, 60, 30)
KEY_PASSWORD = "s3cret"


@dataclass
class PkiFiles:
    ca: Path
    server_cert: Path
    server_key: Path
    client_cert: Path
    client_key: Path  # encrypted with KEY_PASSWORD
    client_key_plain: Path


def _openssl(*args, cwd: Path) -> None:
    subprocess.run(["openssl", *args], cwd=cwd, check=True, capture_output=True)


def make_pki(d: Path) -> PkiFiles:
    if not shutil.which("openssl"):
        raise RuntimeError("openssl not available")
    d.mkdir(parents=True, exist_ok=True)
    _openssl("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", "ca.key", "-out", "ca.pem",
             "-days", "2", "-subj", "/CN=MapForge Test CA", cwd=d)
    (d / "san.cnf").write_text("subjectAltName=DNS:localhost,IP:127.0.0.1\n")
    _openssl("req", "-newkey", "rsa:2048", "-nodes", "-keyout", "server.key", "-out", "server.csr",
             "-subj", "/CN=localhost", cwd=d)
    _openssl("x509", "-req", "-in", "server.csr", "-CA", "ca.pem", "-CAkey", "ca.key", "-CAcreateserial",
             "-out", "server.pem", "-days", "2", "-extfile", "san.cnf", cwd=d)
    _openssl("req", "-newkey", "rsa:2048", "-nodes", "-keyout", "client_plain.key", "-out", "client.csr",
             "-subj", "/CN=Test User", cwd=d)
    _openssl("x509", "-req", "-in", "client.csr", "-CA", "ca.pem", "-CAkey", "ca.key", "-CAcreateserial",
             "-out", "client.pem", "-days", "2", cwd=d)
    _openssl("pkey", "-in", "client_plain.key", "-out", "client.key", "-aes256", "-passout",
             f"pass:{KEY_PASSWORD}", cwd=d)
    return PkiFiles(d / "ca.pem", d / "server.pem", d / "server.key", d / "client.pem", d / "client.key",
                    d / "client_plain.key")


def _png(w: int, h: int) -> bytes:
    arr = np.empty((3, h, w), np.uint8)
    for i, c in enumerate(COLOR):
        arr[i] = c
    with MemoryFile() as m:
        with m.open(driver="PNG", width=w, height=h, count=3, dtype="uint8") as ds:
            ds.write(arr)
        return m.read()


def _geotiff(bbox, w: int, h: int) -> bytes:
    arr = np.empty((3, h, w), np.uint8)
    for i, c in enumerate(COLOR):
        arr[i] = c
    with MemoryFile() as m:
        with m.open(driver="GTiff", width=w, height=h, count=3, dtype="uint8", crs="EPSG:4326",
                    transform=from_bounds(*bbox, w, h)) as ds:
            ds.write(arr)
        return m.read()


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def do_HEAD(self):  # noqa: N802
        self.do_GET(head=True)

    def do_GET(self, head: bool = False):  # noqa: N802
        u = urlparse(self.path)
        q = {k.lower(): v[0] for k, v in parse_qs(u.query).items()}
        self.server.hits.append(u.path)
        if u.path.startswith("/tiles/") and u.path.endswith(".png"):
            body, ctype = _png(256, 256), "image/png"
        elif u.path == "/wms":
            body, ctype = _png(int(q.get("width", 256)), int(q.get("height", 256))), "image/png"
        elif u.path == "/arcgis/ImageServer/exportImage":
            bbox = [float(v) for v in q["bbox"].split(",")]
            w, h = (int(v) for v in q["size"].split(","))
            body, ctype = _geotiff(bbox, w, h), "image/tiff"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not head:
            self.wfile.write(body)


class _Server(ThreadingHTTPServer):
    daemon_threads = True


def serve(pki_dir: Path) -> None:
    """Run in a child process: print the port, then serve forever."""
    httpd = _Server(("127.0.0.1", 0), _Handler)
    httpd.hits = []
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(pki_dir / "server.pem", pki_dir / "server.key")
    ctx.load_verify_locations(pki_dir / "ca.pem")
    ctx.verify_mode = ssl.CERT_REQUIRED
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True, do_handshake_on_connect=False)
    print(httpd.server_address[1], flush=True)
    httpd.serve_forever()


class PkiServer:
    """Runs the server in a separate process so its GDAL use cannot contend with the client's."""

    def __init__(self, pki: PkiFiles):
        self.pki = pki
        self.proc: subprocess.Popen | None = None
        self.url = ""

    def __enter__(self):
        self.proc = subprocess.Popen([sys.executable, __file__, str(self.pki.ca.parent)], stdout=subprocess.PIPE,
                                     text=True)
        port = int(self.proc.stdout.readline())
        self.url = f"https://localhost:{port}"
        return self

    def __exit__(self, *exc):
        self.proc.terminate()
        self.proc.wait(10)


if __name__ == "__main__":
    serve(Path(sys.argv[1]))
