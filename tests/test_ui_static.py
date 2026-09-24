"""The web UI's static files: every script the page loads exists and is served, in dependency order."""
from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from mapforge import sources
from mapforge.app import LOOPBACK_HOSTS, create_app
from mapforge.settings import Settings

STATIC = Path(__file__).resolve().parents[1] / "mapforge" / "static"
ORDER = ["vendor/leaflet/leaflet.js", "core.js", "map.js", "build.js", "jobs.js", "admin.js", "app.js"]


def test_index_loads_scripts_in_dependency_order():
    html = (STATIC / "index.html").read_text()
    scripts = re.findall(r'<script src="([^"]+)"', html)
    assert scripts == ORDER
    for s in scripts:
        assert (STATIC / s).is_file(), s


def test_static_files_served(tmp_path):
    s = Settings(data_dir=tmp_path / "data", library_dirs=[tmp_path / "lib"])
    sources.refresh(s)
    client = TestClient(create_app(s, allowed_hosts=LOOPBACK_HOSTS), base_url="http://localhost:8765")
    page = client.get("/")
    assert page.status_code == 200 and 'id="panel"' in page.text and 'data-view="guided"' in page.text
    for path in ORDER + ["app.css"]:
        r = client.get("/" + path)
        assert r.status_code == 200 and len(r.content) > 100, path


def test_ui_never_injects_unescaped_api_text():
    # Every ${…} interpolated into HTML templates in the UI scripts that carries API/user data
    # must go through esc() (or be a number/format helper). Spot-check the risky fields.
    risky = re.compile(r"\$\{(?:j|l|d|s|e|p|r)\.(?:name|message|label|path|detail|plain_name|explain|description|license|url)\}")
    # Text-only sinks never parse HTML, so interpolating raw text into them is safe.
    text_sinks = ("toast(", "confirm(", ".textContent", "alert(", "prompt(")
    for f in ["build.js", "jobs.js", "admin.js", "map.js"]:
        for n, line in enumerate((STATIC / f).read_text().splitlines(), 1):
            m = risky.search(line)
            if m and not any(sink in line for sink in text_sinks):
                raise AssertionError(f"{f}:{n}: {m.group(0)} is not escaped")
