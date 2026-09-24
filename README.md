# MapForge

A self-hosted web tool that makes **fetching, bounding and converting** map data a
point-and-click job: draw a box, tick the layers you need (FAA charts, high-resolution imagery,
elevation, your own NGA products or PKI-protected services), and get back a package of
map-engine-ready rasters: GeoTIFF / Cloud-Optimized GeoTIFF / MBTiles in EPSG:4326, plus
standard **DTED** cells from any elevation layer. The package layout and notes are aimed at
Kongsberg Geospatial **TerraLens** and similar C2 map engines.

* Runs on one Linux box (or in Docker) and is used from a browser. Works on an air-gapped network
  for local-library data; see [Offline / air-gapped install](#offline--air-gapped-install).
* No system GDAL needed: the `rasterio` wheels bundle GDAL 3.x with the NITF/RPF (CADRG/CIB),
  ECRG, DTED, GTiff, COG, MBTiles, WMS and WMTS drivers.
* Processing is block-based, so areas far larger than RAM work.

---

## Quick start

```bash
git clone <this repo> mapforge && cd mapforge
scripts/install.sh          # creates ./.venv and installs MapForge
scripts/run.sh              # http://127.0.0.1:8765
```

Open `http://127.0.0.1:8765` and follow the three steps on the Build tab:

1. **Area**: draw a box, type W/S/E/N, paste `W,S,E,N`, or give a center plus a radius in NM.
2. **Layers**: tick sources. Leave resolution empty for native, or set metres/pixel. The ◎
   button shows each source's coverage footprints on the map.
3. **Outputs**: GeoTIFF (default), COG, MBTiles, and DTED level 0/1/2 for elevation layers.
   The estimate shows the pixel size and approximate MB of each layer before you build.

Jobs run in the background. The **Jobs** tab shows progress, each layer's result, the package
path on the server, and a **Download .zip** button.

To serve other machines on your LAN, set a token:

```bash
MAPFORGE_TOKEN="$(openssl rand -hex 24)" scripts/run.sh --host 0.0.0.0
```

### Docker

```bash
docker compose up -d --build     # binds 127.0.0.1:8765 by default; see docker-compose.yml
```

The container runs as uid 10001. Host directories mounted at `/data` and `/library` must be
writable by that uid (`sudo chown -R 10001 data library`). The Dockerfile and compose file are
provided but have **not been test-built** in the environment where MapForge was written.

### systemd

```ini
# /etc/systemd/system/mapforge.service
[Unit]
Description=MapForge map packager
After=network-online.target

[Service]
User=mapforge
Environment=MAPFORGE_DATA=/srv/mapforge/data
Environment=MAPFORGE_LIBRARY=/srv/mapforge/library
Environment=MAPFORGE_TOKEN=change-me
ExecStart=/opt/mapforge/.venv/bin/mapforge --host 0.0.0.0 --port 8765
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

---

## Sources

| Group | Source | Notes |
|---|---|---|
| Aeronautical (FAA, public domain) | VFR Sectional, Terminal Area (TAC), Flyway, Helicopter, Caribbean, Grand Canyon | Current editions are discovered automatically from the FAA digital-products pages (56-day cycle). Footprints are indexed with HTTP range requests, and only intersecting charts are downloaded and cached. |
| | IFR Enroute Low / High, Area, Alaska, Pacific, Oceanic (NARC, WATRS) | GeoTIFF editions only. PORC (North Pacific) is skipped because it crosses the antimeridian. |
| Imagery | **USGS NAIP**: 0.3–1 m aerial photography, lower 48 | Chunked `exportImage` from the USGS ImageServer, with retries. |
| | **USGS Imagery Only**: The National Map tiles | Fast for large US areas. Max zoom 16 (~2 m). |
| | **Sentinel-2 cloudless** (EOX), 10 m global, 2016 and 2024 | **Licence:** the 2016 mosaic is CC BY 4.0. **2017 and later mosaics are CC BY-NC-SA 4.0, which means non-commercial use only**; buy a commercial licence from EOX for anything else. |
| Elevation | **Copernicus DEM GLO-30 / GLO-90** | Global DSM, COGs on AWS Open Data. Tiles are cached locally. Free including commercial use (attribution required). |
| | **USGS 3DEP**: 1–10 m, US | Bare-earth elevation, lidar-derived where available. |
| Local library | Anything on disk: CADRG / CIB (`A.TOC`), ECRG (`TOC.xml`), DTED (`.dt0/.dt1/.dt2`), NITF, GeoTIFF, JPEG 2000 | This is where NGA products downloaded with your CAC go. See [docs/nga-and-pki.md](docs/nga-and-pki.md). |
| Custom / controlled | XYZ, WMTS, WMS, ArcGIS ImageServer, ArcGIS tiled MapServer | Optional PKI client certificate, basic auth or header token. Presets exist for NGA GEGD WMTS/WMS. See [docs/nga-and-pki.md](docs/nga-and-pki.md). |

**Chart collars.** FAA sheets carry no neatline data, so MapForge detects each chart's
neatline from its pixels the first time the chart is used, and caches the result. Sectionals are
clipped at the dark whole-minute parallel/meridian bordered by white paper. Other sheets (TACs,
helicopter charts) get a pixel cut of white margins wherever that is unambiguous. Where charts
still overlap, each pixel comes from the chart it sits deepest inside, measured from the clipped
extent. Any side where detection is unsure is left unclipped rather than risk cutting real chart,
so check the seams of unusual sheets (insets, IFR enroute panels) before relying on a mosaic.
The `verify-map-output` project skill renders a contact sheet for exactly that.

---

## Output package

```
<name>/
├── README.txt            layer list (coarsest first), native display scale + suggested scale range, licences
├── manifest.json         machine-readable: bbox, CRS, per-layer resolution, size, inputs (chart editions), files
├── 01_faa-sectional/
│   ├── faa-sectional.tif          GeoTIFF, EPSG:4326, 512×512 tiles, internal overviews + mask
│   ├── faa-sectional_cog.tif      (optional) Cloud-Optimized GeoTIFF
│   ├── faa-sectional.mbtiles      (optional) Web-Mercator tile package
│   └── layer.json
├── 02_usgs-naip/ …
└── 05_copernicus-dem-30/
    ├── copernicus-dem-30.tif      float32 metres, nodata −32767
    └── dted/w078/n38.dt1          standard DTED tree: full 1°×1° cells, correct post spacing per latitude zone
```

Compression is lossless (deflate) for charts and elevation and JPEG q90 for imagery. You can
override this per layer through the API (`compression`: `jpeg|deflate|lzw|none`).

## Loading into Kongsberg TerraLens

Everything is plain, standard data, so TerraLens should read it with its standard raster and
elevation data sources. In short:

* Add each `.tif` as a GeoTIFF raster map layer, in the order listed in `README.txt` (coarse
  first), and set each layer's visible scale range to the "useful range" printed there. This
  gives automatic chart and imagery switching as you zoom.
* Point the DTED / elevation source at the layer's `dted/` folder.
* Exact configuration steps depend on your TerraLens version and SDK. Confirm them against
  Kongsberg's documentation. More detail is in [docs/terralens.md](docs/terralens.md).

---

## Configuration

| Variable | CLI flag | Default | Meaning |
|---|---|---|---|
| `MAPFORGE_DATA` | `--data` | `./data` | Cache, jobs/packages and config. `config/endpoints.json` holds endpoint credentials, so protect this directory. |
| `MAPFORGE_LIBRARY` | `--library` (repeatable) | `$MAPFORGE_DATA/library` | Local library directories, `:`-separated. Uploads go into the first one. |
| `MAPFORGE_HOST` | `--host` | `127.0.0.1` | Bind address. Use `0.0.0.0` to serve the LAN. |
| `MAPFORGE_PORT` | `--port` | `8765` | HTTP port. |
| `MAPFORGE_TOKEN` | – | unset | When set, every `/api` call needs the header `X-MapForge-Token` (the UI prompts for it once). |
| `MAPFORGE_MAX_PIXELS` | – | `2000000000` | Per-layer pixel limit. Larger requests are refused with a hint. |
| `MAPFORGE_WORKERS` | – | `2` | Jobs processed concurrently. |
| `MAPFORGE_USER_AGENT` | – | `MapForge/0.1 …` | User-Agent sent to data providers. |

MapForge has no user accounts. Treat it as a single-team tool: keep it on a trusted network,
behind the token, or behind your own reverse proxy with authentication/TLS.

## Offline / air-gapped install

On a connected machine with the same OS family, CPU architecture and Python minor version as
the target:

```bash
scripts/make-offline-bundle.sh            # -> dist/mapforge-offline.tar.gz (~60 MB)
# or cross-target: PY_VERSION=3.11 PLATFORM=manylinux2014_x86_64 scripts/make-offline-bundle.sh
```

Carry the tarball across (following your media-transfer rules), then:

```bash
tar xzf mapforge-offline.tar.gz && cd mapforge-offline
./install-offline.sh /opt/mapforge/venv     # pip --no-index from the bundled wheels
MAPFORGE_DATA=/srv/mapforge /opt/mapforge/venv/bin/mapforge --host 0.0.0.0
```

On a disconnected network, the public sources are unreachable. The local library and any
endpoints reachable on that network (for example an internal WMS/WMTS) work normally. The UI is
fully self-contained because Leaflet is vendored. Pick **None (offline)** or an internal basemap
in the map's basemap selector.

## Limitations

* **Antimeridian:** boxes must satisfy W < E. Split areas that cross 180°. The FAA PORC chart is
  skipped for the same reason.
* **No CADRG/CIB output:** GDAL can read RPF but not write it (its NITF driver silently writes
  plain NITF), so MapForge outputs GeoTIFF/COG/MBTiles/DTED instead.
* **NAIP seams:** NAIP is flown state by state in different years. Visible colour or season
  changes along quarter-quad lines are in the source data. MapForge doesn't create them.
* **Chart currency:** FAA charts are only valid for their edition. `manifest.json` records every
  edition used. Packages are not for navigation once the edition expires.
* **Resampling:** charts use nearest-neighbour with anti-aliased downsampling, imagery uses
  bilinear/average, and elevation uses bilinear. Upsampling past native resolution adds no
  detail.
* **Licensing and handling:** follow each source's licence and any distribution statement on NGA
  media. Only process controlled data on systems authorised for it.

## Development

```bash
.venv/bin/pip install -e '.[test]'
.venv/bin/python -m pytest -q          # offline tests with synthetic rasters
```

Code map: `mapforge/sources/*` (one module per provider family: `faa`, `services`, `local`,
`custom`), `mapforge/process.py` (mosaic, warp and writers), `mapforge/jobs.py` (queue and
packaging), `mapforge/app.py` (FastAPI) and `mapforge/static/` (UI).

To add a provider, subclass `sources.base.Source` and return `Item`s (anything GDAL can open,
plus a lon/lat footprint) from `items(bbox, res_m, ctx)`. The engine handles everything else.
