---
name: verify-map-output
description: Validate a MapForge output package (GeoTIFF/COG/MBTiles/DTED) and eyeball it via a contact-sheet PNG. Use this after any change to MapForge's processing, sources or writers, after building a package for the user, or whenever someone asks "is the output right / will it load in TerraLens / are the DTED cells valid / why does the map look wrong" — don't declare map output correct without running it.
---

# Verify MapForge output

A package folder (`<data>/jobs/<id>/<name>/`) holds `manifest.json`, `README.txt` and one
folder per layer with a `layer.json`. Correctness has two halves, and both matter:

1. **Structural** — things a map engine rejects or silently misplaces: wrong CRS, untiled
   files, missing overviews (slow zoom-out), lossy compression on charts or elevation, bounds
   off from the requested box, malformed DTED cells.
2. **Visual** — things only a look catches: chart collars/legends bleeding into mosaics, colour
   seams between chunks, wrong zoom level (blurry), black/empty areas, misaligned layers.

## Run it

```bash
~/Projects/mapforge/.venv/bin/python .claude/skills/verify-map-output/scripts/verify_package.py \
    PACKAGE_DIR --sheet /path/to/contact_sheet.png
```

It prints one line per file (`OK` / `WARN` / `FAIL`) and exits non-zero on any FAIL. Add
`--json` for machine-readable output. Then open the contact sheet with the Read tool: layers are
left→right in folder order; elevation is shown as a hillshade.

To produce a package to verify, either submit a job through the API (see `run-mapforge`) or call
`mapforge.jobs.JobManager(Settings(data_dir=...)).submit(spec)` from Python and poll the job
dict until its status leaves `queued`/`running`. Keep test areas small (≈0.1–0.2°) so jobs finish
in a minute or two; reuse a data dir with a warm cache to avoid re-downloading charts.

## What the checks mean

| Check | Why it matters |
|---|---|
| EPSG:4326, tiled | TerraLens/C2 engines load WGS84 geographic rasters natively; untiled TIFFs render slowly |
| Overviews on >1024 px | Without them every zoom-out reads full resolution |
| Charts/elevation lossless | JPEG smears chart text; lossy DEMs corrupt heights |
| Bounds within 1 px of package bbox | Layers of one package must stack exactly |
| Valid-pixel % | <95% usually means the area left the source's coverage (e.g. NAIP outside CONUS) — WARN, not always wrong |
| DTED post grid | Level 0/1/2 = 30″/3″/1″ lat spacing, longitude spacing coarsens by latitude zone (×2 above 50°, ×3 above 70°, …); extent must be a whole-degree cell, post-centred; path `dted/wNNN/nNN.dtL` |
| COG layout | `_cog.tif` must really be a COG, and should keep the primary file's lossless compression |

## Reading the contact sheet

Look specifically for: white/legend strips inside chart mosaics (collar bleed), straight-edged
brightness blocks in imagery (chunk seams — but NAIP also has genuine year seams on its
0.0625° quarter-quad grid), pixelated charts (resolution far finer than native), blurred imagery
(wrong zoom picked), and a DEM hillshade whose terrain matches the imagery.

When reporting, state which checks ran, the FAIL/WARN lines verbatim, and what the sheet showed.
