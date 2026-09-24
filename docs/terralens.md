# Using MapForge packages with Kongsberg TerraLens

> **Scope.** These are practical notes for consuming the package's standard formats. TerraLens
> is an SDK, and how map data is configured (API calls, configuration files, map-data
> preprocessing or caching tools, supported-format lists) varies between versions and between
> the applications built on it. Check every step below against the Kongsberg Geospatial
> documentation for your version, or with Kongsberg support. Nothing here comes from Kongsberg.

## What the package contains

| Item | Format | Why |
|---|---|---|
| `NN_<source>/<source>.tif` | GeoTIFF, **EPSG:4326** (WGS84 geographic), 512×512 tiles, internal overviews, internal mask, 8-bit RGB (charts/imagery) or float32 metres (elevation) | Geographic WGS84 is the common denominator for military map engines, and it sits alongside CADRG/CIB/DTED without reprojection. Overviews keep zoomed-out display fast. |
| `NN_<source>/<source>_cog.tif` | Cloud-Optimized GeoTIFF | Same data, reorganised for streaming or HTTP range access. |
| `NN_<source>/<source>.mbtiles` | Web-Mercator JPEG tiles | For web or mobile viewers. Usually not needed for TerraLens. |
| `NN_<source>/dted/wNNN/nNN.dtL` | DTED Level 0/1/2 | Standard DTED directory tree, full 1°×1° cells, correct latitude-zone post spacing. Voids are −32767. |
| `README.txt`, `manifest.json` | Text / JSON | Layer order, resolution, native display scale, suggested scale range, source editions, licences. |

## Suggested setup

1. **Raster layers, coarse to fine.** `README.txt` lists layers from the coarsest resolution to
   the finest. Add them to the map in that order so the finer layers draw on top, for example
   IFR enroute, then sectional, then TAC, then Sentinel-2, then NAIP.
2. **Scale ranges.** For each layer, `README.txt` prints a *native display scale* (the map scale
   at which one raster pixel is one screen pixel at 96 dpi) and a *useful range* (roughly ½× to
   8× native). Setting each layer's visible scale range to that window gives automatic switching
   between charts and imagery as the operator zooms, the same way CADRG series switch.
3. **Elevation.** Point the engine's DTED / elevation data source at the `dted/` folder. The
   folder already has the conventional `dted/<lon>/<lat>.dtN` layout, so it can often be merged
   into an existing DTED tree by copying the `wNNN/` folders across. The float32 GeoTIFF of the
   same layer is there if your workflow prefers GeoTIFF elevation.
4. **Preprocessing / caching.** Some TerraLens deployments preprocess or cache raster data into
   an internal format before display. If yours does, run that step on the GeoTIFFs. They're
   plain, tiled GeoTIFFs with nothing MapForge-specific in them.
5. **Chart editions.** `manifest.json` → `layers[].inputs` records the FAA edition of every chart
   used. Rebuild the package (same box, same layers) after each 56-day cycle. MapForge picks up
   the new editions automatically.

## If a layer doesn't load

* Check the file with GDAL: `gdalinfo <file>.tif`. It should report EPSG:4326, `Block=512x512`,
  overviews, and (for RGB) a per-dataset mask.
* If your TerraLens build only accepts uncompressed or LZW TIFFs, rebuild the layer with
  `compression: "lzw"` or `"none"` through the API (`POST /api/jobs`, per-layer `compression`),
  or convert it with `gdal_translate -co COMPRESS=LZW -co TILED=YES`.
* JPEG-compressed imagery uses YCbCr with an internal mask. Some older readers mishandle that;
  LZW or deflate avoids it at the cost of larger files.
* If you need CADRG/CIB rather than GeoTIFF: GDAL, and therefore MapForge, cannot *write*
  RPF. Use your organisation's RPF production tools on the GeoTIFF output if that format is
  mandatory.
