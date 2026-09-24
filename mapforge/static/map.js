"use strict";
// Map: basemaps, offline graticule, drawing / clearing the area box, coverage footprints.

const LARGE_AREA_KM2 = 20000;
const map = L.map("map", { worldCopyJump: false }).setView(st.mapView?.center || [38.9, -77.03], st.mapView?.zoom || 9);
map.on("moveend", () => { const c = map.getCenter(); st.mapView = { center: [c.lat, c.lng], zoom: map.getZoom() }; persist(); });

const basemaps = {
  osm: L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", { maxZoom: 19, attribution: "© OpenStreetMap" }),
  usgs: L.tileLayer("https://basemap.nationalmap.gov/arcgis/rest/services/USGSImageryOnly/MapServer/tile/{z}/{y}/{x}", { maxZoom: 16, attribution: "USGS" }),
  topo: L.tileLayer("https://basemap.nationalmap.gov/arcgis/rest/services/USGSTopo/MapServer/tile/{z}/{y}/{x}", { maxZoom: 16, attribution: "USGS" }),
  none: L.layerGroup(),
};

// Lat/lon graticule: always-available reference when there is no basemap.
const graticule = L.layerGroup();
function drawGraticule() {
  graticule.clearLayers();
  if (!map.hasLayer(graticule)) return;
  const b = map.getBounds(), span = Math.max(b.getEast() - b.getWest(), b.getNorth() - b.getSouth());
  const step = [30, 10, 5, 2, 1, 0.5, 0.25, 0.1, 0.05, 0.01].find((s) => span / s >= 4 && span / s <= 12) || 0.01;
  const dp = step < 0.1 ? 2 : step < 1 ? 1 : 0;
  const w = Math.max(-180, Math.floor(b.getWest() / step) * step), e = Math.min(180, Math.ceil(b.getEast() / step) * step);
  const s = Math.max(-90, Math.floor(b.getSouth() / step) * step), n = Math.min(90, Math.ceil(b.getNorth() / step) * step);
  const style = { color: "#7f8a85", weight: 0.7, opacity: 0.7, interactive: false };
  for (let x = w; x <= e + 1e-9; x += step) {
    graticule.addLayer(L.polyline([[s, x], [n, x]], style));
    graticule.addLayer(L.marker([b.getNorth(), x], { interactive: false, icon: L.divIcon({ className: "gl", html: `${Math.abs(x).toFixed(dp)}°${x < 0 ? "W" : "E"}`, iconAnchor: [-2, -2] }) }));
  }
  for (let y = s; y <= n + 1e-9; y += step) {
    graticule.addLayer(L.polyline([[y, w], [y, e]], style));
    graticule.addLayer(L.marker([y, b.getWest()], { interactive: false, icon: L.divIcon({ className: "gl", html: `${Math.abs(y).toFixed(dp)}°${y < 0 ? "S" : "N"}`, iconAnchor: [-2, 14] }) }));
  }
}
map.on("moveend", drawGraticule);

function setBasemap(name, remember = true) {
  if (!basemaps[name]) name = "osm";
  Object.values(basemaps).forEach((l) => map.removeLayer(l));
  basemaps[name].addTo(map);
  $("#basemap").value = name;
  if (name === "none") { graticule.addTo(map); drawGraticule(); } else map.removeLayer(graticule);
  if (remember) store.set("basemap", name);
  watchBasemap(name);
}
// If the chosen basemap cannot load a single tile, fall back to the grid rather than a blank map.
function watchBasemap(name) {
  const layer = basemaps[name];
  if (!layer.on || name === "none") return;
  let ok = 0, bad = 0;
  const onLoad = () => ok++, onErr = () => {
    if (++bad >= 4 && ok === 0 && $("#basemap").value === name) {
      setBasemap("none", false);
      toast("Basemap tiles unreachable — showing a lat/lon grid instead.", 6000);
    }
  };
  layer.off("tileload tileerror");
  layer.on("tileload", onLoad).on("tileerror", onErr);
}
$("#basemap").addEventListener("change", (e) => setBasemap(e.target.value));
setBasemap(store.get("basemap", "osm"), false);

// ---------------------------------------------------------------------------- area box
let rect = null;
const RECT_STYLE = { color: "#e4572e", weight: 2.5, fillColor: "#e4572e", fillOpacity: 0.08, dashArray: "6 4" };
const areaListeners = [];
const onAreaChange = (fn) => areaListeners.push(fn);
const fireArea = () => areaListeners.forEach((fn) => fn(st.bbox));

function areaStats(b = st.bbox) {
  if (!b) return null;
  const [w, s, e, n] = b;
  const wkm = (e - w) * 111.32 * Math.cos(((s + n) / 2) * Math.PI / 180), hkm = (n - s) * 111.32;
  return { wkm, hkm, km2: wkm * hkm, wnm: wkm * 1000 / NM, hnm: hkm * 1000 / NM };
}
function areaSummaryHTML() {
  const a = areaStats();
  if (!a) return '<span class="muted">No area yet.</span>';
  return `<b>${a.wkm.toFixed(1)} × ${a.hkm.toFixed(1)} km</b> <span class="muted">(${a.wnm.toFixed(1)} × ${a.hnm.toFixed(1)} NM · ${Math.round(a.km2).toLocaleString()} km²)</span>`
    + (a.km2 > LARGE_AREA_KM2 ? `<div class="hint">Large area: keep imagery at a low level of detail, or split it into several jobs.</div>` : "");
}

function setBBox(b, { fit = false, quiet = false } = {}) {
  if (!b) return false;
  let [w, s, e, n] = b.map(Number);
  if (![w, s, e, n].every(Number.isFinite) || w >= e || s >= n) {
    if (!quiet) toast("That box isn't valid: west must be less than east, and south less than north.");
    return false;
  }
  w = Math.max(-180, w); e = Math.min(180, e); s = Math.max(-90, s); n = Math.min(90, n);
  st.bbox = [w, s, e, n].map((v) => +v.toFixed(6));
  const bounds = [[st.bbox[1], st.bbox[0]], [st.bbox[3], st.bbox[2]]];
  if (rect) rect.setBounds(bounds); else rect = L.rectangle(bounds, RECT_STYLE).addTo(map);
  if (fit) map.fitBounds(bounds, { padding: [30, 30] });
  $("#mapClear").classList.remove("hidden");
  persist(); fireArea();
  return true;
}
function clearBBox() {
  if (drawing) setDrawing(false);
  if (rect) { map.removeLayer(rect); rect = null; }
  if (!st.bbox) return;
  st.bbox = null;
  $("#mapClear").classList.add("hidden");
  persist(); fireArea();
  toast("Area cleared");
}
function bboxFromCenter(lat, lon, radiusNm) {
  if (!Number.isFinite(lat) || !Number.isFinite(lon) || !(radiusNm > 0)) { toast("Enter a latitude, a longitude and a radius in NM."); return false; }
  if (Math.abs(lat) > 89) { toast("That latitude is too close to the pole."); return false; }
  const r = radiusNm * NM, dLat = r / 111320, dLon = r / (111320 * Math.cos(lat * Math.PI / 180));
  return setBBox([lon - dLon, lat - dLat, lon + dLon, lat + dLat], { fit: true });
}
function bboxFromView() {
  const b = map.getBounds();
  return setBBox([b.getWest(), b.getSouth(), b.getEast(), b.getNorth()]);
}

// Drawing with pointer events (mouse, pen and touch): press, drag, release.
let drawing = false, start = null;
const mapEl = map.getContainer();
const drawListeners = [];
function setDrawing(on) {
  drawing = on;
  mapEl.classList.toggle("drawing", on);
  $("#drawHint").classList.toggle("hidden", !on);
  if (on) { map.dragging.disable(); map.touchZoom.disable(); map.boxZoom.disable(); }
  else { map.dragging.enable(); map.touchZoom.enable(); map.boxZoom.enable(); start = null; }
  drawListeners.forEach((fn) => fn(on));
}
mapEl.addEventListener("pointerdown", (e) => {
  if (!drawing || e.button > 0) return;
  start = map.mouseEventToLatLng(e);
  mapEl.setPointerCapture?.(e.pointerId);
  e.preventDefault();
});
mapEl.addEventListener("pointermove", (e) => {
  if (!drawing || !start) return;
  const b = L.latLngBounds(start, map.mouseEventToLatLng(e));
  if (rect) rect.setBounds(b); else rect = L.rectangle(b, RECT_STYLE).addTo(map);
});
mapEl.addEventListener("pointerup", (e) => {
  if (!drawing || !start) return;
  const b = L.latLngBounds(start, map.mouseEventToLatLng(e));
  setDrawing(false);
  if (b.getNorth() - b.getSouth() < 1e-5 || b.getEast() - b.getWest() < 1e-5) { restoreRect(); return; }
  setBBox([b.getWest(), b.getSouth(), b.getEast(), b.getNorth()]);
});
function restoreRect() {
  if (st.bbox) setBBox(st.bbox, { quiet: true });
  else if (rect) { map.removeLayer(rect); rect = null; }
}
$("#drawHint .x").addEventListener("click", () => { setDrawing(false); restoreRect(); });
$("#mapClear").addEventListener("click", clearBBox);
document.addEventListener("keydown", (e) => {
  const typing = e.target.closest("input, textarea, select, [contenteditable]");
  if (e.key === "Escape" && drawing) { setDrawing(false); restoreRect(); }
  else if ((e.key === "Delete" || e.key === "Backspace") && !typing && st.bbox && $("#tab-build").classList.contains("active")) {
    e.preventDefault(); clearBBox();
  }
});

// ---------------------------------------------------------------------- coverage footprints
const coverageCache = {};   // source id -> [{label,bbox}]
const coverageLayers = {};  // source id -> visible leaflet layer
const PALETTE = ["#2f6f5e", "#c2410c", "#1d4ed8", "#9333ea", "#b45309", "#0f766e", "#be123c", "#4d7c0f", "#0369a1", "#7c3aed"];
const colorFor = (id) => PALETTE[[...id].reduce((a, c) => (a * 31 + c.charCodeAt(0)) >>> 0, 7) % PALETTE.length];
const intersects = (a, b) => !(b[2] <= a[0] || b[0] >= a[2] || b[3] <= a[1] || b[1] >= a[3]);
const coverageListeners = [];

async function fetchCoverage(id) {
  if (!coverageCache[id]) coverageCache[id] = await api(`/api/sources/${encodeURIComponent(id)}/coverage`);
  return coverageCache[id];
}
// true / false when the footprints are known, null when unknown.
function hasDataHere(id) {
  const cov = coverageCache[id];
  if (!st.bbox || !cov) return null;
  return cov.some((c) => intersects(st.bbox, c.bbox));
}
async function toggleCoverage(id, name) {
  if (coverageLayers[id]) { map.removeLayer(coverageLayers[id]); delete coverageLayers[id]; renderLegend(); coverageListeners.forEach((f) => f()); return; }
  try {
    const cov = await fetchCoverage(id);
    const color = colorFor(id);
    coverageLayers[id] = L.layerGroup(cov.map((c) => L.rectangle([[c.bbox[1], c.bbox[0]], [c.bbox[3], c.bbox[2]]],
      { color, weight: 1.2, fillOpacity: 0.05 }).bindTooltip(esc(c.label), { sticky: true }))).addTo(map);
    coverageLayers[id]._name = name;
    toast(`${cov.length} sheet(s)/file(s) shown on the map`);
  } catch (e) { toast("Couldn't load coverage: " + e.message, 6000); }
  renderLegend(); coverageListeners.forEach((f) => f());
}
function renderLegend() {
  const ids = Object.keys(coverageLayers);
  const el = $("#legend");
  el.classList.toggle("hidden", !ids.length);
  el.innerHTML = ids.length ? `<div class="lt">Where data exists</div>` + ids.map((id) => `<div class="li">
      <span class="sw" style="background:${colorFor(id)}"></span>${esc(coverageLayers[id]._name || id)}
      <span class="muted">${coverageCache[id]?.length ?? 0}</span><button class="x" data-id="${esc(id)}" aria-label="Hide">×</button></div>`).join("") : "";
  $$("#legend .x").forEach((b) => b.addEventListener("click", () => toggleCoverage(b.dataset.id)));
}
// Fetch footprints in the background (cheap once cached) so sources with no data can be flagged.
let covQueue = Promise.resolve();
function prefetchCoverage(srcs) {
  if (!st.bbox) return;
  for (const s of srcs.filter((x) => x.coverage && !coverageCache[x.id])) {
    covQueue = covQueue.then(() => fetchCoverage(s.id).then(() => coverageListeners.forEach((f) => f())).catch(() => {}));
  }
}

if (st.bbox) setBBox(st.bbox, { quiet: true });
