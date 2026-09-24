"""Map servers drop connections; one dropped tile must not fail a whole layer."""
from __future__ import annotations

import http.server
import re
import socket
import struct
import threading

import numpy as np
import pytest
import rasterio
from rasterio.io import MemoryFile

from mapforge import process
from mapforge.geo import BBox
from mapforge.process import LayerOptions, OutputOptions, build_layer
from mapforge.settings import Settings
from mapforge.sources.base import Context
from mapforge.sources import xyz
from mapforge.sources.services import TileService

TILE_RE = re.compile(r"^/tiles/(\d+)/(\d+)/(\d+)\.png$")


def png_tile() -> bytes:
    with MemoryFile() as mem:
        with mem.open(driver="PNG", width=256, height=256, count=3, dtype="uint8") as ds:
            ds.write(np.full((3, 256, 256), 140, np.uint8))
        return mem.read()


class FlakyTiles(http.server.BaseHTTPRequestHandler):
    """Serves one grey tile everywhere. `policy(path, n)` decides whether request n drops."""

    png = b""
    policy = staticmethod(lambda path, n: False)
    seen: dict = {}
    lock = threading.Lock()

    def log_message(self, *a):
        pass

    def do_GET(self):
        with FlakyTiles.lock:
            n = FlakyTiles.seen.get(self.path, 0) + 1
            FlakyTiles.seen[self.path] = n
            total = sum(FlakyTiles.seen.values())
        if not TILE_RE.match(self.path):
            self.send_error(404)
            return
        if FlakyTiles.policy(self.path, n, total):
            # A real TCP reset (RST), as in "Recv failure: Connection reset by peer" from USGS:
            # SO_LINGER with a zero timeout makes close() abort the connection.
            self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            self.connection.close()
            self.close_connection = True
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(FlakyTiles.png)))
        self.end_headers()
        self.wfile.write(FlakyTiles.png)


@pytest.fixture()
def flaky(monkeypatch):
    FlakyTiles.png, FlakyTiles.seen = png_tile(), {}
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FlakyTiles)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr(process, "NET_RETRY_DELAYS_S", (0, 0, 0))
    monkeypatch.setattr(xyz, "RETRY_DELAYS_S", (0, 0, 0))
    yield f"http://127.0.0.1:{srv.server_address[1]}/tiles/{{z}}/{{x}}/{{y}}.png"
    srv.shutdown()


@pytest.fixture()
def settings(tmp_path):
    return Settings(data_dir=tmp_path / "data", library_dirs=[tmp_path / "lib"])


def tile_source(url):
    return TileService("flaky", "Flaky tiles", url, "Imagery", max_zoom=14, default_res_m=20)


BOX = BBox(-77.05, 38.88, -77.00, 38.92)


@pytest.mark.parametrize("mode", ["kongsberg", "clipped"])
def test_first_request_for_every_tile_drops_but_layer_completes(settings, tmp_path, flaky, mode):
    # A burst of TCP resets: after the probe, every tile is reset on its first 3 requests and served
    # on the 4th. GDAL does not retry a reset connection itself (it only retries HTTP error
    # codes), so only MapForge's back-off rounds get through — this is what failed a real USGS
    # Imagery job with "Read failed". The fixture allows 4 rounds; production allows 5 over ~50 s.
    FlakyTiles.policy = staticmethod(lambda path, n, total: total > 3 and n <= 3)
    res = build_layer(tile_source(flaky), BOX, LayerOptions(res_m=20), OutputOptions(overviews=False),
                      tmp_path / "out", Context(settings), "flaky", mode=mode)
    assert res["status"] == "ok", res
    tifs = [p for p in (tmp_path / "out").glob("*.tif")]
    assert tifs
    with rasterio.open(tifs[0]) as ds:
        assert (ds.read(1) == 140).mean() > 0.99  # no holes where tiles were dropped
    assert max(FlakyTiles.seen.values()) >= 4  # tiles needed several MapForge rounds


def test_server_that_keeps_dropping_gives_a_plain_error(settings, tmp_path, flaky):
    # The probe succeeds (first few requests), then the server drops everything.
    FlakyTiles.policy = staticmethod(lambda path, n, total: total > 3)
    with pytest.raises(RuntimeError, match=r"could not be downloaded after \d+ tries.*run the job again"):
        build_layer(tile_source(flaky), BOX, LayerOptions(res_m=20), OutputOptions(overviews=False),
                    tmp_path / "out", Context(settings), "flaky", mode="clipped")


def test_non_network_read_errors_are_not_retried():
    calls = []

    def boom():
        calls.append(1)
        raise rasterio.errors.RasterioIOError("Read failed: corrupt TIFF directory")

    with pytest.raises(rasterio.errors.RasterioIOError):
        process.read_with_retry(boom, delays=(0, 0))
    assert calls == [1]


def test_bar_keeps_moving_during_a_slow_tile_download(settings, tmp_path, flaky, monkeypatch):
    """The user saw the bar sit at 48% with a frozen ETA while one big read downloaded tiles.
    Services are now read in small blocks, with a progress update after each."""
    import time as _time

    slow = FlakyTiles.do_GET

    def slow_get(self):
        _time.sleep(0.03)  # a slow map server
        slow(self)

    monkeypatch.setattr(FlakyTiles, "do_GET", slow_get)
    FlakyTiles.policy = staticmethod(lambda path, n, total: False)
    events = []
    ctx = Context(settings, progress=lambda m, f=None: events.append((_time.monotonic(), m, f)))
    res = build_layer(tile_source(flaky), BBox(-77.10, 38.84, -76.95, 38.96), LayerOptions(res_m=5),
                      OutputOptions(overviews=False), tmp_path / "out", ctx, "slow", mode="clipped")
    assert res["status"] == "ok"
    parts = [m for _, m, _ in events if "part " in m]
    assert len(parts) >= 4 and parts[-1].endswith(f"part {len(parts)} of {len(parts)}"), parts
    fracs = [f for _, _, f in events if f is not None]
    assert all(b >= a - 1e-9 for a, b in zip(fracs, fracs[1:])), "bar went backwards"
    times = [t for t, _, _ in events]
    assert max(b - a for a, b in zip(times, times[1:])) < 2.0  # no long silences
