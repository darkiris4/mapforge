"use strict";
// MapForge UI — plain JS, no build step, works offline (Leaflet is vendored).

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const NM = 1852;
const LARGE_AREA_KM2 = 20000;

// localStorage can throw (private mode, blocked storage) — every access goes through these.
const store = {
  get(k, d = null) { try { const v = localStorage.getItem("mapforge." + k); return v === null ? d : JSON.parse(v); } catch (_) { return d; } },
  set(k, v) { try { localStorage.setItem("mapforge." + k, JSON.stringify(v)); } catch (_) {} },
};

let token = store.get("token", "");

async function api(path, opts = {}) {
  const headers = { ...(opts.headers || {}) };
  if (token) headers["X-MapForge-Token"] = token;
  const init = { ...opts, headers };
  if (opts.json !== undefined) { headers["Content-Type"] = "application/json"; init.body = JSON.stringify(opts.json); }
  delete init.json;
  const r = await fetch(path, init);
  if (r.status === 401) {
    const t = prompt("This MapForge server requires an access token:");
    if (!t) throw new Error("Access token required");
    token = t; store.set("token", t);
    return api(path, opts);
  }
  const body = r.headers.get("content-type")?.includes("json") ? await r.json() : await r.text();
  if (!r.ok) throw new Error(body?.detail || body || r.statusText);
  return body;
}

function toast(msg, ms = 3500) {
  const t = $("#toast"); t.textContent = msg; t.classList.remove("hidden");
  clearTimeout(toast._t); toast._t = setTimeout(() => t.classList.add("hidden"), ms);
}

async function copyText(text) {
  try { await navigator.clipboard.writeText(text); }
  catch (_) { // clipboard API needs a secure context; fall back for plain-http LAN use
    const ta = document.createElement("textarea"); ta.value = text; document.body.appendChild(ta);
    ta.select(); document.execCommand("copy"); ta.remove();
  }
  toast("Copied");
}

const fmtDur = (s) => {
  s = Math.max(0, Math.round(s));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return h ? `${h}h ${m}m` : m ? `${m}m ${sec}s` : `${sec}s`;
};

// ---------------------------------------------------------------------------------- tabs
$$("nav button").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));
function showTab(name) {
  if (!$("#tab-" + name)) name = "build";
  if (location.hash.slice(1) !== name) history.replaceState(null, "", "#" + name);
  $$("nav button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  $$(".tab").forEach((t) => t.classList.toggle("active", t.id === "tab-" + name));
  if (name === "build") setTimeout(() => map.invalidateSize(), 50);
  if (name === "jobs") loadJobs();
  if (name === "endpoints") loadEndpoints().catch((e) => toast(e.message, 6000));
  if (name === "library") loadLibrary().catch((e) => toast(e.message, 6000));
}

// ----------------------------------------------------------------------------------- map
const saved = store.get("state", {});
const map = L.map("map", { worldCopyJump: false }).setView(saved.view?.center || [38.9, -77.03], saved.view?.zoom || 9);
map.on("moveend", () => persist());
const basemaps = {
  osm: L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", { maxZoom: 19, attribution: "© OpenStreetMap" }),
  usgs: L.tileLayer("https://basemap.nationalmap.gov/arcgis/rest/services/USGSImageryOnly/MapServer/tile/{z}/{y}/{x}", { maxZoom: 16, attribution: "USGS" }),
  topo: L.tileLayer("https://basemap.nationalmap.gov/arcgis/rest/services/USGSTopo/MapServer/tile/{z}/{y}/{x}", { maxZoom: 16, attribution: "USGS" }),
  none: L.layerGroup(),
};

// Lat/lon graticule: always-available reference when there is no basemap (offline networks).
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
// If the chosen online basemap cannot load a single tile, we are probably offline: fall back.
function watchBasemap(name) {
  const layer = basemaps[name];
  if (!layer.on || name === "none") return;
  let ok = 0, bad = 0;
  const onLoad = () => ok++, onErr = () => {
    if (++bad >= 4 && ok === 0 && $("#basemap").value === name) {
      setBasemap("none", false);
      toast("Basemap tiles unreachable (offline network?) — showing a lat/lon grid instead.", 6000);
    }
  };
  layer.off("tileload tileerror");
  layer.on("tileload", onLoad).on("tileerror", onErr);
}
$("#basemap").addEventListener("change", (e) => setBasemap(e.target.value));
setBasemap(store.get("basemap", "osm"), false);

let bbox = null;
let rect = null;
const RECT_STYLE = { color: "#e4572e", weight: 2, fillOpacity: 0.06, dashArray: "6 4" };

function setBBox(b, fit = false, quiet = false) {
  if (!b) return;
  let [w, s, e, n] = b.map(Number);
  if (![w, s, e, n].every(Number.isFinite) || w >= e || s >= n) { if (!quiet) toast("Invalid box: need W < E and S < N"); return; }
  w = Math.max(-180, w); e = Math.min(180, e); s = Math.max(-90, s); n = Math.min(90, n);
  bbox = [w, s, e, n].map((v) => +v.toFixed(6));
  [["#bW", 0], ["#bS", 1], ["#bE", 2], ["#bN", 3]].forEach(([id, i]) => ($(id).value = bbox[i]));
  const bounds = [[s, w], [n, e]];
  if (rect) rect.setBounds(bounds); else rect = L.rectangle(bounds, RECT_STYLE).addTo(map);
  if (fit) map.fitBounds(bounds, { padding: [30, 30] });
  const midLat = ((s + n) / 2) * Math.PI / 180;
  const wkm = (e - w) * 111.32 * Math.cos(midLat), hkm = (n - s) * 111.32;
  const km2 = wkm * hkm;
  $("#areaInfo").innerHTML = `${wkm.toFixed(1)} × ${hkm.toFixed(1)} km (${(wkm * 1000 / NM).toFixed(1)} × ${(hkm * 1000 / NM).toFixed(1)} NM) · ${Math.round(km2).toLocaleString()} km²`
    + (km2 > LARGE_AREA_KM2 ? `<div class="hint">Large area — keep imagery layers at coarse resolution (≥ 10 m) or split into several jobs.</div>` : "");
  persist();
  markCoverage();
  scheduleEstimate();
}

// Box drawing with pointer events (mouse, pen and touch): press, drag, release.
let drawing = false, start = null;
const mapEl = map.getContainer();
function setDrawing(on) {
  drawing = on;
  $("#drawBtn").classList.toggle("primary", !on);
  $("#drawBtn").textContent = on ? "✕ Cancel drawing" : "▭ Draw box";
  mapEl.classList.toggle("drawing", on);
  if (on) { map.dragging.disable(); map.touchZoom.disable(); map.boxZoom.disable(); toast("Press and drag on the map to draw the area"); }
  else { map.dragging.enable(); map.touchZoom.enable(); map.boxZoom.enable(); start = null; }
}
$("#drawBtn").addEventListener("click", () => setDrawing(!drawing));
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
  if (b.getNorth() - b.getSouth() < 1e-5 || b.getEast() - b.getWest() < 1e-5) { if (bbox) setBBox(bbox, false, true); return; }
  setBBox([b.getWest(), b.getSouth(), b.getEast(), b.getNorth()]);
});
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && drawing) { setDrawing(false); if (bbox) setBBox(bbox, false, true); } });
$("#viewBtn").addEventListener("click", () => {
  const b = map.getBounds();
  setBBox([b.getWest(), b.getSouth(), b.getEast(), b.getNorth()]);
});
["#bW", "#bS", "#bE", "#bN"].forEach((id) => $(id).addEventListener("change", () => {
  const v = ["#bW", "#bS", "#bE", "#bN"].map((i) => $(i).value);
  if (v.every((x) => x !== "")) setBBox(v, true);
}));
$("#centerBtn").addEventListener("click", () => {
  const lat = parseFloat($("#cLat").value), lon = parseFloat($("#cLon").value), r = parseFloat($("#cRad").value) * NM;
  if (!Number.isFinite(lat) || !Number.isFinite(lon) || !(r > 0)) return toast("Enter lat, lon and a radius in NM");
  if (Math.abs(lat) > 89) return toast("Latitude too close to the pole");
  const dLat = r / 111320, dLon = r / (111320 * Math.cos(lat * Math.PI / 180));
  setBBox([lon - dLon, lat - dLat, lon + dLon, lat + dLat], true);
});
$("#paste").addEventListener("change", (e) => {
  const v = e.target.value.split(/[\s,;]+/).filter(Boolean).map(Number);
  if (v.length === 4 && v.every(Number.isFinite)) setBBox(v, true); else toast("Paste four numbers: W, S, E, N");
});

// ------------------------------------------------------------------------------- sources
let sources = [];
const selected = new Map(Object.entries(saved.selected || {})); // id -> {res_m}
const coverageCache = {};   // id -> [{label,bbox}]
const coverageLayers = {};  // id -> leaflet layer (visible footprints)
const PALETTE = ["#2f6f5e", "#c2410c", "#1d4ed8", "#9333ea", "#b45309", "#0f766e", "#be123c", "#4d7c0f", "#0369a1", "#7c3aed"];
const colorFor = (id) => PALETTE[[...id].reduce((a, c) => (a * 31 + c.charCodeAt(0)) >>> 0, 7) % PALETTE.length];

async function loadSources() {
  sources = await api("/api/sources");
  for (const id of [...selected.keys()]) if (!sources.some((s) => s.id === id)) selected.delete(id);
  renderSources();
  markCoverage();
  scheduleEstimate();
}

function intersects(a, b) { return !(b[2] <= a[0] || b[0] >= a[2] || b[3] <= a[1] || b[1] >= a[3]); }

function renderSources() {
  const f = $("#srcFilter").value.toLowerCase();
  const groups = {};
  sources.filter((s) => !f || (s.name + s.group + s.description).toLowerCase().includes(f))
    .forEach((s) => (groups[s.group] ||= []).push(s));
  const order = (g) => (g.startsWith("Aeronautical") ? 0 : g.startsWith("Imagery") ? 1 : g.startsWith("Elevation") ? 2 : g.startsWith("Local") ? 3 : 4);
  const html = Object.keys(groups).sort((a, b) => order(a) - order(b) || a.localeCompare(b)).map((g) => `
    <div class="group"><div class="gh">${esc(g)}</div>
      ${groups[g].map((s) => `
        <div class="src ${selected.has(s.id) ? "checked" : ""}" data-id="${esc(s.id)}">
          <div class="top">
            <label><input type="checkbox" ${selected.has(s.id) ? "checked" : ""}>${esc(s.name)}</label>
            ${s.access === "pki" ? '<span class="tag pki">PKI</span>' : s.access === "local" ? '<span class="tag local">local</span>' : ""}
            ${s.kind === "elevation" ? '<span class="tag">elev</span>' : ""}
            <span class="tag nocov-tag">no data here</span>
            ${s.coverage ? `<button class="eye ${coverageLayers[s.id] ? "on" : ""}" title="Show coverage on map" aria-label="Show coverage"
               style="--c:${colorFor(s.id)}">◎</button>` : ""}
          </div>
          <div class="desc">${esc(s.description)}</div>
          <div class="opts">
            <label>Resolution (m/px)<input type="number" step="any" min="${s.min_res_m}" placeholder="native ${s.default_res_m}"
              value="${esc(selected.get(s.id)?.res_m ?? "")}" class="res"></label>
            <label title="${esc(s.license)}">Licence<input readonly value="${esc(s.license)}"></label>
          </div>
        </div>`).join("")}
    </div>`).join("");
  $("#sources").innerHTML = html || '<p class="muted">No sources match.</p>';
  $$("#sources .src").forEach((el) => {
    const id = el.dataset.id;
    $("input[type=checkbox]", el).addEventListener("change", (e) => {
      if (e.target.checked) selected.set(id, { res_m: "" }); else selected.delete(id);
      el.classList.toggle("checked", e.target.checked);
      persist(); scheduleEstimate();
    });
    $(".res", el).addEventListener("change", (e) => {
      if (selected.has(id)) selected.get(id).res_m = e.target.value;
      persist(); scheduleEstimate();
    });
    $(".eye", el)?.addEventListener("click", () => toggleCoverage(id));
  });
  markCoverage(false);
}
$("#srcFilter").addEventListener("input", renderSources);

async function fetchCoverage(id) {
  if (!coverageCache[id]) coverageCache[id] = await api(`/api/sources/${encodeURIComponent(id)}/coverage`);
  return coverageCache[id];
}

async function toggleCoverage(id) {
  const btn = $(`.src[data-id="${CSS.escape(id)}"] .eye`);
  if (coverageLayers[id]) { map.removeLayer(coverageLayers[id]); delete coverageLayers[id]; btn?.classList.remove("on"); renderLegend(); return; }
  if (btn) btn.textContent = "…";
  try {
    const cov = await fetchCoverage(id);
    const color = colorFor(id);
    const g = L.layerGroup(cov.map((c) => L.rectangle([[c.bbox[1], c.bbox[0]], [c.bbox[3], c.bbox[2]]],
      { color, weight: 1.2, fillOpacity: 0.05 }).bindTooltip(esc(c.label), { sticky: true })));
    coverageLayers[id] = g.addTo(map);
    btn?.classList.add("on");
    toast(`${cov.length} footprint(s)`);
    markCoverage(false);
  } catch (e) { toast("Coverage failed: " + e.message, 6000); }
  if (btn) btn.textContent = "◎";
  renderLegend();
}

function renderLegend() {
  const ids = Object.keys(coverageLayers);
  const el = $("#legend");
  el.classList.toggle("hidden", !ids.length);
  el.innerHTML = ids.map((id) => {
    const s = sources.find((x) => x.id === id);
    return `<div class="li"><span class="sw" style="background:${colorFor(id)}"></span>${esc(s?.name || id)}
      <span class="muted">${coverageCache[id]?.length ?? 0}</span><button class="x" data-id="${esc(id)}" aria-label="Hide">×</button></div>`;
  }).join("");
  $$("#legend .x").forEach((b) => b.addEventListener("click", () => toggleCoverage(b.dataset.id)));
}

// Grey out sources with known footprints that miss the box. Coverage is fetched in the
// background for sources that advertise it (FAA index + local library are cheap once cached).
let covQueue = Promise.resolve();
function markCoverage(fetchMissing = true) {
  for (const s of sources) {
    const el = $(`.src[data-id="${CSS.escape(s.id)}"]`);
    if (!el) continue;
    const cov = coverageCache[s.id];
    el.classList.toggle("nocov", !!(bbox && cov && !cov.some((c) => intersects(bbox, c.bbox))));
  }
  if (!fetchMissing || !bbox) return;
  for (const s of sources.filter((x) => x.coverage && !coverageCache[x.id])) {
    covQueue = covQueue.then(() => fetchCoverage(s.id).then(() => markCoverage(false)).catch(() => {}));
  }
}

// ------------------------------------------------------------------------------ estimate
function spec() {
  return {
    name: $("#jobName").value.trim() || "area",
    bbox,
    layers: [...selected].map(([source, o]) => ({ source, res_m: o.res_m ? +o.res_m : null })),
    outputs: { geotiff: $("#oTif").checked, cog: $("#oCog").checked, mbtiles: $("#oMbt").checked, dted_level: $("#oDted").value || null },
  };
}
function persist() {
  if (typeof selected === "undefined") return;
  const c = map.getCenter();
  store.set("state", {
    bbox, selected: Object.fromEntries(selected), view: { center: [c.lat, c.lng], zoom: map.getZoom() },
    outputs: { tif: $("#oTif").checked, cog: $("#oCog").checked, mbt: $("#oMbt").checked, dted: $("#oDted").value },
    name: $("#jobName").value,
  });
}
let estT;
function scheduleEstimate() { clearTimeout(estT); estT = setTimeout(estimate, 300); }
["#oTif", "#oCog", "#oMbt", "#oDted", "#jobName"].forEach((id) => $(id).addEventListener("change", () => { persist(); scheduleEstimate(); }));
// ArcGIS ImageServer exports (e.g. NAIP) are one slow server request per 2000 px chunk.
function exportNote(l) {
  if (!l.requests) return "";
  const t = l.fetch_minutes >= 90 ? `${(l.fetch_minutes / 60).toFixed(1)} h` : `${Math.max(1, l.fetch_minutes)} min`;
  const cls = l.fetch_minutes >= 30 ? "bad" : "muted";
  return `<br><span class="${cls}">↳ ${l.requests.toLocaleString()} server export request(s), roughly ${t} to download` +
    (l.fetch_minutes >= 30 ? " — consider a coarser resolution, a smaller box, or USGS Imagery tiles" : "") + "</span>";
}
async function estimate() {
  const ok = bbox && selected.size;
  $("#buildBtn").disabled = !ok;
  if (!ok) { $("#estimate").textContent = bbox ? "Select at least one layer." : "Draw or enter an area."; return; }
  try {
    const e = await api("/api/estimate", { method: "POST", json: spec() });
    const noData = e.layers.filter((l) => $(`.src[data-id="${CSS.escape(l.source)}"]`)?.classList.contains("nocov"));
    $("#estimate").innerHTML = e.layers.map((l) =>
      `${esc(l.name)}: ${l.width.toLocaleString()}×${l.height.toLocaleString()} px @ ${l.res_m} m ≈ ${l.est_mb} MB${l.too_big ? ' <b class="bad">too large — use a coarser resolution</b>' : ""}${exportNote(l)}`).join("<br>")
      + `<br><b>≈ ${e.total_mb.toLocaleString()} MB total</b> (GeoTIFF estimate)`
      + (noData.length ? `<div class="hint">${noData.map((l) => esc(l.name)).join(", ")}: no known data in this area.</div>` : "")
      + (e.total_mb > 4000 ? `<div class="hint">This is a big package; building may take a long time.</div>` : "");
    $("#buildBtn").disabled = e.layers.some((l) => l.too_big);
  } catch (err) { $("#estimate").innerHTML = `<span class="bad">${esc(err.message)}</span>`; $("#buildBtn").disabled = true; }
}
$("#buildBtn").addEventListener("click", async () => {
  $("#buildBtn").disabled = true;
  try {
    const j = await api("/api/jobs", { method: "POST", json: spec() });
    toast(`Job ${j.name} queued`);
    showTab("jobs");
  } catch (e) { toast(e.message, 6000); }
  scheduleEstimate();
});

// ---------------------------------------------------------------------------------- jobs
let pollT;
function jobTiming(j) {
  const now = Date.now() / 1000;
  if (j.status === "running" && j.started) {
    const el = now - j.started;
    const eta = j.progress > 0.03 ? (el / j.progress) * (1 - j.progress) : null;
    return `elapsed ${fmtDur(el)}${eta !== null ? ` · ~${fmtDur(eta)} left` : ""}`;
  }
  if (j.finished && j.started) return `took ${fmtDur(j.finished - j.started)}`;
  return "";
}
async function loadJobs() {
  clearTimeout(pollT);
  let jobs;
  try { jobs = await api("/api/jobs"); } catch (e) { $("#jobs").innerHTML = `<p class="bad">${esc(e.message)}</p>`; return; }
  const active = jobs.filter((j) => j.status === "running" || j.status === "queued").length;
  $("#jobBadge").textContent = active; $("#jobBadge").classList.toggle("hidden", !active);
  const tq = token ? "?token=" + encodeURIComponent(token) : "";
  $("#jobs").innerHTML = jobs.length ? jobs.map((j) => {
    const running = ["running", "queued"].includes(j.status);
    const cur = j.current;
    return `
    <div class="job">
      <div class="head">
        <div><span class="title">${esc(j.name)}</span> <span class="st ${esc(j.status)}">${esc(j.status)}</span>
          <span class="muted">${new Date(j.created * 1000).toLocaleString()} ${esc(jobTiming(j))}</span></div>
        <div class="row">
          ${["done", "partial"].includes(j.status) ? `<a class="btn primary small" href="/api/jobs/${esc(j.id)}/download${tq}">Download .zip</a>` : ""}
          ${running ? `<button class="small" data-cancel="${esc(j.id)}">Cancel</button>` : `<button class="small" data-del="${esc(j.id)}">Delete</button>`}
        </div>
      </div>
      <div class="muted">${esc(j.message)} · box ${j.spec.bbox.map((v) => v.toFixed(4)).join(", ")}</div>
      ${running ? `<div class="bar" title="overall"><div style="width:${(j.progress * 100).toFixed(1)}%"></div></div>
        ${cur ? `<div class="muted">Layer ${cur.index + 1}/${cur.count}: ${esc(cur.name)}</div>
          <div class="bar thin" title="current layer"><div style="width:${((j.layer_progress || 0) * 100).toFixed(1)}%"></div></div>` : ""}` : ""}
      ${j.package ? `<div class="muted pathrow">On server: <span class="path">${esc(j.package)}</span>
        <button class="small" data-copy="${esc(j.package)}">Copy path</button></div>` : ""}
      <ul class="layers">${j.layers.map((l) => `<li class="${l.status === "ok" ? "" : "bad"}">
        <b>${esc(l.name)}</b> — ${l.status === "ok"
          ? `${l.res_m} m/px, ${l.width}×${l.height}, ${l.coverage_pct ?? "?"}% covered · ${l.files.map(esc).slice(0, 4).join(", ")}${l.files.length > 4 ? ` … (+${l.files.length - 4})` : ""}`
          : esc(l.message || l.status)}</li>`).join("")}</ul>
    </div>`;
  }).join("") : '<p class="muted">No jobs yet — build one from the Build tab.</p>';
  $$("[data-cancel]").forEach((b) => b.addEventListener("click", async () => {
    b.disabled = true; await api(`/api/jobs/${b.dataset.cancel}/cancel`, { method: "POST" }).catch((e) => toast(e.message)); loadJobs();
  }));
  $$("[data-del]").forEach((b) => b.addEventListener("click", async () => {
    if (!confirm("Delete this job and its package files?")) return;
    await api(`/api/jobs/${b.dataset.del}`, { method: "DELETE" }).catch((e) => toast(e.message, 5000)); loadJobs();
  }));
  $$("[data-copy]").forEach((b) => b.addEventListener("click", () => copyText(b.dataset.copy)));
  if (active) pollT = setTimeout(loadJobs, 2000);
}

// ----------------------------------------------------------------------------- endpoints
let epMeta = null;
async function loadEndpoints() {
  epMeta = await api("/api/endpoints");
  if (!$("#epType").options.length) {
    $("#epType").innerHTML = Object.entries(epMeta.types).map(([k, v]) => `<option value="${esc(k)}">${esc(v)}</option>`).join("");
    $("#epPreset").innerHTML = '<option value="">—</option>' + epMeta.presets.map((p, i) => `<option value="${i}">${esc(p.label)}</option>`).join("");
  }
  $("#endpoints").innerHTML = epMeta.endpoints.length ? epMeta.endpoints.map((e) => `
    <div class="ep"><div class="row" style="justify-content:space-between">
      <div><b>${esc(e.name)}</b> <span class="tag">${esc(e.type)}</span> ${e.auth?.type && e.auth.type !== "none" ? `<span class="tag pki">${esc(e.auth.type)}</span>` : ""}</div>
      <div class="row"><button class="small" data-edit="${esc(e.id)}">Edit</button><button class="small" data-rm="${esc(e.id)}">Remove</button></div></div>
      <div class="muted path">${esc(e.url)}</div></div>`).join("")
    : '<p class="muted">No custom endpoints yet. Use the form to add NGA GEGD or another service.</p>';
  $$("[data-edit]").forEach((b) => b.addEventListener("click", () => fillForm(epMeta.endpoints.find((e) => e.id === b.dataset.edit))));
  $$("[data-rm]").forEach((b) => b.addEventListener("click", async () => {
    if (!confirm("Remove this endpoint?")) return;
    await api(`/api/endpoints/${encodeURIComponent(b.dataset.rm)}`, { method: "DELETE" });
    loadEndpoints(); loadSources();
  }));
}
function fillForm(e) {
  const f = $("#epForm"); f.reset();
  $$("[name]", f).forEach((el) => { if (el.type === "hidden") el.value = ""; });
  const flat = { ...e }; Object.entries(e.auth || {}).forEach(([k, v]) => (flat["auth." + k] = v));
  $$("[name]", f).forEach((el) => { if (flat[el.name] != null) el.value = flat[el.name]; });
  $("#epTitle").textContent = e.id ? `Edit endpoint “${e.name}”` : "Add endpoint";
  $("#epResult").textContent = "";
  syncAuth();
  f.scrollIntoView({ behavior: "smooth", block: "start" });
}
function readForm() {
  const o = { auth: {} };
  const authType = $("#authType").value;
  $$("#epForm [name]").forEach((el) => {
    const v = el.value.trim(); if (v === "") return;
    if (el.name.startsWith("auth.")) {
      const k = el.name.slice(5);
      const box = el.closest("[data-auth]");
      if (box && box.dataset.auth !== authType) return; // ignore fields of other auth types
      o.auth[k] = v;
    } else o[el.name] = el.type === "number" ? +v : v;
  });
  return o;
}
function syncAuth() { const t = $("#authType").value; $$("[data-auth]").forEach((d) => (d.style.display = d.dataset.auth === t ? "block" : "none")); }
$("#authType").addEventListener("change", syncAuth);
$("#epPreset").addEventListener("change", (e) => {
  const p = epMeta?.presets[e.target.value]; if (!p) return;
  fillForm({ type: p.type, group: p.group, auth: p.auth, name: "" });
  $("#epPreset").value = e.target.value;
  $("#epHint").textContent = p.hint;
});
$("#epReset").addEventListener("click", () => {
  $("#epForm").reset(); $("[name=id]").value = ""; $("#epHint").textContent = ""; $("#epResult").textContent = "";
  $("#epTitle").textContent = "Add endpoint"; syncAuth();
});
$("#epTest").addEventListener("click", async () => {
  $("#epResult").textContent = "Testing…";
  const t = { ...readForm() }; if (bbox) t.test_bbox = bbox;
  try { const r = await api("/api/endpoints/test", { method: "POST", json: t }); $("#epResult").innerHTML = `<span class="${r.ok ? "good" : "bad"}">${r.ok ? "✔" : "✖"} ${esc(r.message)}</span>`; }
  catch (e) { $("#epResult").innerHTML = `<span class="bad">✖ ${esc(e.message)}</span>`; }
});
$("#epForm").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  try {
    const r = await api("/api/endpoints", { method: "POST", json: readForm() });
    $("[name=id]").value = r.id;
    $("#epTitle").textContent = "Edit endpoint";
    $("#epResult").innerHTML = '<span class="good">Saved — it now appears as a layer on the Build tab.</span>';
    loadEndpoints(); loadSources();
  } catch (e) { $("#epResult").innerHTML = `<span class="bad">✖ ${esc(e.message)}</span>`; }
});

// ------------------------------------------------------------------------------- library
let libWasRunning = false;
async function loadLibrary() {
  const l = await api("/api/library");
  $("#libDirs").innerHTML = l.dirs.map((d) => `<span class="path">${esc(d)}</span>`).join(" ");
  if (!$("#libStatus").dataset.uploading) $("#libStatus").textContent = l.scan.message || "";
  $("#libTable tbody").innerHTML = l.products.length ? l.products.map((p) =>
    `<tr><td>${esc(p.name)}</td><td>${esc(p.kind)}</td><td>${p.files}</td></tr>`).join("")
    : '<tr><td colspan="3" class="muted">Nothing indexed yet.</td></tr>';
  if (l.scan.running) setTimeout(() => loadLibrary().catch(() => {}), 1500);
  else if (libWasRunning) { Object.keys(coverageCache).filter((k) => k.startsWith("local-")).forEach((k) => delete coverageCache[k]); loadSources(); }
  libWasRunning = l.scan.running;
}
$("#rescanBtn").addEventListener("click", async () => { await api("/api/library/rescan", { method: "POST" }); libWasRunning = true; loadLibrary(); });
// XHR (not fetch) so large NGA archives show upload progress.
$("#uploadFile").addEventListener("change", (e) => {
  const file = e.target.files[0]; if (!file) return;
  const st = $("#libStatus"), bar = $("#uploadBar");
  const xhr = new XMLHttpRequest();
  // Raw streaming PUT: the server writes straight into the library (no /tmp copy).
  xhr.open("PUT", "/api/library/upload?filename=" + encodeURIComponent(file.name));
  xhr.setRequestHeader("Content-Type", "application/octet-stream");
  if (token) xhr.setRequestHeader("X-MapForge-Token", token);
  st.dataset.uploading = "1"; bar.classList.remove("hidden");
  xhr.upload.onprogress = (ev) => {
    if (!ev.lengthComputable) return;
    const pct = (ev.loaded / ev.total) * 100;
    $("div", bar).style.width = pct.toFixed(1) + "%";
    st.textContent = pct < 100 ? `Uploading ${file.name}… ${pct.toFixed(0)}%` : `Extracting ${file.name}…`;
  };
  xhr.onloadend = () => {
    delete st.dataset.uploading; bar.classList.add("hidden"); e.target.value = "";
    if (xhr.status >= 200 && xhr.status < 300) { st.textContent = `Uploaded ${file.name}; scanning…`; libWasRunning = true; loadLibrary(); }
    else { let m = xhr.statusText || "network error"; try { m = JSON.parse(xhr.responseText).detail; } catch (_) {} st.textContent = "Upload failed: " + m; }
  };
  xhr.send(file);
});

// ---------------------------------------------------------------------------------- boot
if (saved.outputs) {
  $("#oTif").checked = saved.outputs.tif ?? true; $("#oCog").checked = !!saved.outputs.cog;
  $("#oMbt").checked = !!saved.outputs.mbt; if (saved.outputs.dted !== undefined) $("#oDted").value = saved.outputs.dted;
}
if (saved.name) $("#jobName").value = saved.name;
if (saved.bbox) setBBox(saved.bbox, false, true);
loadSources().catch((e) => toast("Could not load sources: " + e.message, 8000));
loadJobs().catch(() => {});
syncAuth();
estimate();
if (location.hash.length > 1) showTab(location.hash.slice(1));
window.addEventListener("hashchange", () => showTab(location.hash.slice(1) || "build"));
