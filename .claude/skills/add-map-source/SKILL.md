---
name: add-map-source
description: Add a new data provider (chart series, imagery service, elevation dataset, NGA/controlled endpoint type) to MapForge as a Source plugin, with registry wiring and tests. Use this whenever the user wants MapForge to pull from a new place — "add Esri/Maxar/Planet/NOAA charts/ECRG/another WMTS", "support this server", "can it fetch X" — or when extending an existing source family. For a one-off URL the user can simply paste into the Endpoints tab, prefer that and say so.
---

# Add a map source to MapForge

## First decide: code or configuration?

Most services don't need code. The **Endpoints tab** (`sources/custom.py`) already turns a URL
into a source for: XYZ/TMS tiles, WMTS capabilities, WMS GetMap, ArcGIS ImageServer and ArcGIS
tiled MapServer — with none / PKI client-cert / basic / header auth. If the new provider is one of
those, add it as a user endpoint, or (if it should ship built-in) add one entry to
`public_service_sources()` in `sources/services.py` reusing `TileService` / `ArcGISImageServer`.

Write a new class only when the provider needs discovery or custom fetching (like the FAA scraper
in `sources/faa.py`, or Copernicus tile naming in `CopernicusDEM`).

## The contract (`mapforge/sources/base.py`)

A `Source` answers one question: *which rasters cover this box?* It never mosaics or reprojects —
`process.build_layer` warps and composites whatever `Item`s it gets.

```python
class MySource(Source):
    id = "my-source"                # stable, URL-safe; used in job specs and folder names
    name = "Readable name"
    group = "Imagery (public)"      # UI grouping; aeronautical / imagery / elevation / local / custom
    kind = "rgb"                    # "rgb" (8-bit 3-band out) or "elevation" (float metres, DTED-able)
    access = "public"               # public | pki | local  (drives the UI badge)
    license = "..."                 # shown in UI and written to the package README — be accurate
    default_res_m = 10.0            # used when the user leaves resolution blank
    min_res_m = 5.0                 # finest resolution worth requesting
    resampling = "bilinear"         # "nearest" for charts (crisp linework), bilinear otherwise

    def items(self, bbox, res_m, ctx) -> list[Item]: ...
    def has_coverage(self): return True          # optional: footprints on the map
    def coverage(self, ctx): return [{"label": ..., "bbox": [w, s, e, n]}]
```

`Item(path, footprint, label, gdal_env, native_res_m)`:
- `path` — anything `rasterio.open` accepts: local file, `/vsicurl/URL`, `/vsizip/...`, a GDAL
  WMS XML file, `WMTS:url,layer=...`, an RPF/ECRG subdataset string.
- `footprint` — lon/lat `BBox`; drives selection and which item "owns" overlapping pixels
  (read `render()` in `process.py` for the current overlap rule before changing it).
- `native_res_m` — real data resolution; leave `None` for tile pyramids whose finest zoom
  is not the true resolution (otherwise "native" output becomes needlessly huge).
- `gdal_env` — per-item GDAL config such as auth (`Auth.gdal_env()`), merged into the layer env.

`ctx` gives you `ctx.settings.cache_dir` (cache anything reusable there), `ctx.download(url, dest)`
(resumable, progress, cancel-aware), `ctx.http(auth)` (httpx client with UA + auth),
`ctx.progress(msg)` and `ctx.check()` (call in loops so Cancel works).

## Patterns that have worked

- **Prefer local files for repeated reads.** Remote random reads inside deflated zips and
  many-small-range COG reads were each 10–70× slower than downloading once to the cache.
- **Discover, don't hard-code editions.** FAA charts change every 56 days; scrape the index
  page and cache the footprint index keyed by URL so a new edition re-indexes only what changed.
- **Cheap footprints.** Read metadata sidecars (FGDC `.htm`) or TIFF headers via range requests
  instead of downloading products just to learn their extent.
- **Tile services**: rely on overview/zoom selection in `open_item` and the WMS prefetch in
  `render` — don't pre-download whole pyramids.
- **Chunked exports** (ImageServer-like): keep requests ≤2000 px, retry 5xx with backoff, cache
  chunks by a hash of URL+bbox+size.
- **One bad product must not sink the layer**: catch per-item failures, report via
  `ctx.progress`, and skip.

## Wire it up

1. Add the class (or catalogue entry) and include it in `sources/__init__.py:refresh()`.
2. Add an offline test in `tests/` — build synthetic GeoTIFFs with rasterio, point your source
   at them (see `FileSource` in `tests/test_core.py`), and call `build_layer` on a small box.
   Mock HTTP with a local server or monkeypatched `ctx.http` rather than hitting the internet.
3. Run `~/Projects/mapforge/.venv/bin/python -m pytest -q`.
4. Do one real end-to-end build on a small area and run the `verify-map-output` skill on it;
   look at the contact sheet.
5. Add the source to the README's source catalogue with its licence.
