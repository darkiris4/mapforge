# MapForge UI audit (September 2026) — as an ISR mission-system sysadmin who is not a GIS person

Base: master d5411d8. Method: walked every tab (Build, Jobs, Endpoints/NGA, Local library) in
Firefox (1440×900 and 390 px, light + dark) and read each control's code path.
P1 = blocks or misleads a non-GIS user; P2 = friction / needs GIS knowledge; P3 = polish.

## P1 — blocks or misleads
1. **No way to remove a drawn box.** It can only be replaced by redrawing: no Clear button, no
   Delete key. It is also persisted, so it comes back after a reload. (user example)
2. **No size or time shown when a layer is picked.** Nothing appears on the layer. The one estimate
   paragraph at the bottom gives pixel counts and an output-MB guess, with no download size, no
   download time and nothing about cached data. The only time hint is for ArcGIS exports. (user
   example; this is why the El Paso NAIP job looked "stuck")
3. **No choice of what to produce.** Output is always reprojected and mosaicked for Kongsberg; there
   is no way to get clipped native files or the original source files. (user example)
4. **The first screen asks GIS questions.** It opens with "West/South/East/North", "Resolution
   (m/px)", "GeoTIFF (EPSG:4326, tiled + overviews)", "Cloud-Optimized GeoTIFF", "MBTiles" and
   "DTED Level 1 (3″)". There is no path that asks in plain words, e.g. "how detailed?".
5. **Resolution defaults are invisible.** An empty field means "native", shown only as a
   placeholder. Typing 1 into NAIP creates a ~3-hour job with no warning at that moment.
6. **Delivery is one zip link.** There is no "copy to the share", no split for removable media, and
   no checksums to verify a transfer. Those are core sysadmin tasks.

## P2 — friction / needs GIS knowledge
7. Sources (19+) are grouped by provider ("Aeronautical — FAA (public)"), not by need (VFR charts,
   IFR charts, imagery, terrain). Names like "Copernicus DEM GLO-30" mean nothing to a non-GIS user.
8. The licence is a read-only input box inside each layer card, so it looks like a form field.
9. The "◎" coverage button has no visible label; its meaning is only in a tooltip.
10. The estimate crams px dimensions, m/px and MB into one line per layer. The total says
    "(GeoTIFF estimate)" without saying whether it includes downloads.
11. When Build is disabled, the reason isn't shown next to the button.
12. There is no review or confirmation before a large or slow job.
13. Jobs: no per-layer download, no re-run or tweak, and failures show raw exception text (e.g. a
    504 URL dump).
14. There is no glossary for DTED, COG, MBTiles, EPSG:4326, overviews or neatline.
15. Center + radius (the natural way to get an area from a tasking) is hidden in a collapsed section.
16. "No data here" greying only works for sources with coverage data. Others, such as NAIP outside
    CONUS, look selectable.

## P3 — polish
17. The box outline is hard to see on the imagery basemap.
18. "Use map view" silently replaces an existing box.
19. The numeric box fields give no feedback until all four are filled.
20. Mobile: the Build button is far down the page.
21. Dark mode: warning text is low-contrast.
22. Several actions (coverage loaded, job queued) give toast-only feedback.

## Addressed in this round (frontend)
- **Guided mode:** default on first visit, optional via a header toggle. Steps: Where → What
  (plain categories) → How detailed (named levels) → What to produce (3 modes) → How to deliver →
  Review (per-layer and total download size and time, package size, warnings in plain words).
- **Advanced mode:** Clear box (button plus Delete/Backspace/Esc), size/time/cache status on each
  selected layer, output-mode selector, formats shown only where relevant, glossary tooltips, the
  reason shown next to a disabled Build button, and a confirmation for big or slow jobs.
- **Jobs:** per-layer downloads, save to server folder, split for media, delivery status, SHA256
  info, re-run with the same settings, and plain-language failure hints.
