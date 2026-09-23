"use strict";
// MapForge UI — plain JS, no build step, works offline (Leaflet is vendored).

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const NM = 1852;

let token = "";
try { token = localStorage.getItem("mapforge.token") || ""; } catch (_) {}

async function api(path, opts = {}) {
  const headers = { ...(opts.headers || {}) };
  if (token) headers["X-MapForge-Token"] = token;
  if (opts.json !== undefined) { headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(opts.json); }
  const r = await fetch(path, { ...opts, headers });
  if (r.status === 401) {
    token = prompt("This MapForge server requires an access token:") || "";
    try { localStorage.setItem("mapforge.token", token); } catch (_) {}
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

// ---------------------------------------------------------------------------------- tabs
$$("nav button").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));
function showTab(name) {
  if (!$("#tab-" + name)) name = "build";
  if (location.hash.slice(1) !== name) history.replaceState(null, "", "#" + name);
  $$("nav button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  $$(".tab").forEach((t) => t.classList.toggle("active", t.id === "tab-" + name));
  if (name === "build") setTimeout(() => map.invalidateSize(), 50);
  if (name === "jobs") loadJobs();
  if (name === "endpoints") loadEndpoints();
  if (name === "library") loadLibrary();
}

// ----------------------------------------------------------------------------------- map
const map = L.map("map", { worldCopyJump: false }).setView([38.9, -77.03], 9);
const basemaps = {
  osm: L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", { maxZoom: 19, attribution: "© OpenStreetMap" }),
  usgs: L.tileLayer("https://basemap.nationalmap.gov/arcgis/rest/services/USGSImageryOnly/MapServer/tile/{z}/{y}/{x}", { maxZoom: 16, attribution: "USGS" }),
  topo: L.tileLayer("https://basemap.nationalmap.gov/arcgis/rest/services/USGSTopo/MapServer/tile/{z}/{y}/{x}", { maxZoom: 16, attribution: "USGS" }),
  none: L.layerGroup(),
};
let base = "osm";
try { base = localStorage.getItem("mapforge.basemap") || "osm"; } catch (_) {}
$("#basemap").value = base;
basemaps[base].addTo(map);
$("#basemap").addEventListener("change", (e) => {
  Object.values(basemaps).forEach((l) => map.removeLayer(l));
  basemaps[e.target.value].addTo(map);
  try { localStorage.setItem("mapforge.basemap", e.target.value); } catch (_) {}
});

let bbox = null;
let rect = null;
const coverageLayers = {};

function setBBox(b, fit = false) {
  if (!b) return;
  let [w, s, e, n] = b.map(Number);
  if (![w, s, e, n].every(Number.isFinite) || w >= e || s >= n) return toast("Invalid box: need W < E and S < N");
  w = Math.max(-180, w); e = Math.min(180, e); s = Math.max(-90, s); n = Math.min(90, n);
  bbox = [w, s, e, n].map((v) => +v.toFixed(6));
  [["#bW", 0], ["#bS", 1], ["#bE", 2], ["#bN", 3]].forEach(([id, i]) => ($(id).value = bbox[i]));
  const bounds = [[s, w], [n, e]];
  if (rect) rect.setBounds(bounds);
  else rect = L.rectangle(bounds, { color: "#e4572e", weight: 2, fillOpacity: 0.06, dashArray: "6 4" }).addTo(map);
  if (fit) map.fitBounds(bounds, { padding: [30, 30] });
  const midLat = ((s + n) / 2) * Math.PI / 180;
  const wkm = ((e - w) * 111.32 * Math.cos(midLat)).toFixed(1), hkm = ((n - s) * 111.32).toFixed(1);
  $("#areaInfo").textContent = `${wkm} × ${hkm} km  (${(wkm * 1000 / NM).toFixed(1)} × ${(hkm * 1000 / NM).toFixed(1)} NM)`;
  scheduleEstimate();
}

// Box drawing (no plugin: press, drag, release)
let drawing = false, start = null;
$("#drawBtn").addEventListener("click", () => {
  drawing = !drawing;
  $("#drawBtn").classList.toggle("primary", !drawing);
  map.getContainer().classList.toggle("drawing", drawing);
  if (drawing) { map.dragging.disable(); toast("Click and drag on the map to draw the area"); }
  else map.dragging.enable();
});
map.on("mousedown", (e) => { if (drawing) start = e.latlng; });
map.on("mousemove", (e) => {
  if (!drawing || !start) return;
  const b = L.latLngBounds(start, e.latlng);
  if (rect) rect.setBounds(b); else rect = L.rectangle(b, { color: "#e4572e", weight: 2, fillOpacity: 0.06, dashArray: "6 4" }).addTo(map);
});
map.on("mouseup", (e) => {
  if (!drawing || !start) return;
  const b = L.latLngBounds(start, e.latlng);
  start = null; drawing = false; map.dragging.enable();
  map.getContainer().classList.remove("drawing"); $("#drawBtn").classList.add("primary");
  if (b.getNorth() - b.getSouth() < 1e-5) return;
  setBBox([b.getWest(), b.getSouth(), b.getEast(), b.getNorth()]);
});
$("#viewBtn").addEventListener("click", () => {
  const b = map.getBounds();
  setBBox([b.getWest(), b.getSouth(), b.getEast(), b.getNorth()]);
});
["#bW", "#bS", "#bE", "#bN"].forEach((id) => $(id).addEventListener("change", () =>
  setBBox(["#bW", "#bS", "#bE", "#bN"].map((i) => $(i).value), true)));
$("#centerBtn").addEventListener("click", () => {
  const lat = +$("#cLat").value, lon = +$("#cLon").value, r = +$("#cRad").value * NM;
  if (!Number.isFinite(lat) || !Number.isFinite(lon) || !(r > 0)) return toast("Enter lat, lon and radius");
  const dLat = r / 111320, dLon = r / (111320 * Math.cos(lat * Math.PI / 180));
  setBBox([lon - dLon, lat - dLat, lon + dLon, lat + dLat], true);
});
$("#paste").addEventListener("change", (e) => {
  const v = e.target.value.split(/[\s,;]+/).filter(Boolean).map(Number);
  if (v.length === 4) setBBox(v, true); else toast("Paste four numbers: W, S, E, N");
});

// ------------------------------------------------------------------------------- sources
let sources = [];
const selected = new Map(); // id -> {res_m}

async function loadSources() {
  sources = await api("/api/sources");
  renderSources();
}

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
            ${s.coverage ? `<button class="eye ${coverageLayers[s.id] ? "on" : ""}" title="Show coverage">◎</button>` : ""}
          </div>
          <div class="desc">${esc(s.description)}</div>
          <div class="opts">
            <label>Resolution (m/px)<input type="number" step="any" min="${s.min_res_m}" placeholder="native ${s.default_res_m}"
              value="${esc(selected.get(s.id)?.res_m ?? "")}" class="res"></label>
            <label title="${esc(s.license)}">Licence<input disabled value="${esc(s.license)}"></label>
          </div>
        </div>`).join("")}
    </div>`).join("");
  $("#sources").innerHTML = html || '<p class="muted">No sources match.</p>';
  $$("#sources .src").forEach((el) => {
    const id = el.dataset.id;
    $("input[type=checkbox]", el).addEventListener("change", (e) => {
      if (e.target.checked) selected.set(id, { res_m: "" }); else selected.delete(id);
      el.classList.toggle("checked", e.target.checked);
      scheduleEstimate();
    });
    $(".res", el).addEventListener("change", (e) => {
      if (selected.has(id)) selected.get(id).res_m = e.target.value;
      scheduleEstimate();
    });
    $(".eye", el)?.addEventListener("click", () => toggleCoverage(id, $(".eye", el)));
  });
}
$("#srcFilter").addEventListener("input", renderSources);

async function toggleCoverage(id, btn) {
  if (coverageLayers[id]) { map.removeLayer(coverageLayers[id]); delete coverageLayers[id]; btn.classList.remove("on"); return; }
  btn.textContent = "…";
  try {
    const cov = await api(`/api/sources/${encodeURIComponent(id)}/coverage`);
    const g = L.layerGroup(cov.map((c) => L.rectangle([[c.bbox[1], c.bbox[0]], [c.bbox[3], c.bbox[2]]],
      { color: "#2f6f5e", weight: 1, fillOpacity: 0.04 }).bindTooltip(c.label, { sticky: true })));
    coverageLayers[id] = g.addTo(map);
    btn.classList.add("on");
    toast(`${cov.length} footprint(s)`);
  } catch (e) { toast("Coverage failed: " + e.message, 6000); }
  btn.textContent = "◎";
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
let estT;
function scheduleEstimate() { clearTimeout(estT); estT = setTimeout(estimate, 300); }
["#oTif", "#oCog", "#oMbt", "#oDted"].forEach((id) => $(id).addEventListener("change", scheduleEstimate));
async function estimate() {
  const ok = bbox && selected.size;
  $("#buildBtn").disabled = !ok;
  if (!ok) { $("#estimate").textContent = bbox ? "Select at least one layer." : "Draw or enter an area."; return; }
  try {
    const e = await api("/api/estimate", { method: "POST", json: spec() });
    $("#estimate").innerHTML = e.layers.map((l) =>
      `${esc(l.name)}: ${l.width.toLocaleString()}×${l.height.toLocaleString()} px @ ${l.res_m} m ≈ ${l.est_mb} MB${l.too_big ? ' <b class="hint">too large</b>' : ""}`).join("<br>")
      + `<br><b>≈ ${e.total_mb} MB total</b> (GeoTIFF estimate)`;
    $("#buildBtn").disabled = e.layers.some((l) => l.too_big);
  } catch (err) { $("#estimate").textContent = err.message; }
}
$("#buildBtn").addEventListener("click", async () => {
  try {
    const j = await api("/api/jobs", { method: "POST", json: spec() });
    toast(`Job ${j.name} queued`);
    showTab("jobs");
  } catch (e) { toast(e.message, 6000); }
});

// ---------------------------------------------------------------------------------- jobs
let pollT;
async function loadJobs() {
  clearTimeout(pollT);
  const jobs = await api("/api/jobs");
  const active = jobs.filter((j) => j.status === "running" || j.status === "queued").length;
  $("#jobBadge").textContent = active; $("#jobBadge").classList.toggle("hidden", !active);
  $("#jobs").innerHTML = jobs.length ? jobs.map((j) => `
    <div class="job">
      <div class="head">
        <div><span class="title">${esc(j.name)}</span> <span class="st ${esc(j.status)}">${esc(j.status)}</span>
          <span class="muted">${new Date(j.created * 1000).toLocaleString()}</span></div>
        <div class="row">
          ${["done", "partial"].includes(j.status) ? `<a href="/api/jobs/${j.id}/download${token ? "?token=" + encodeURIComponent(token) : ""}"><button class="primary small">Download .zip</button></a>` : ""}
          ${["running", "queued"].includes(j.status) ? `<button class="small" data-cancel="${j.id}">Cancel</button>` : ""}
          <button class="small" data-del="${j.id}">Delete</button>
        </div>
      </div>
      <div class="muted">${esc(j.message)} · box ${j.spec.bbox.map((v) => v.toFixed(4)).join(", ")}</div>
      ${["running", "queued"].includes(j.status) ? `<div class="bar"><div style="width:${(j.progress * 100).toFixed(1)}%"></div></div>` : ""}
      ${j.package ? `<div class="muted">On server: <span class="path">${esc(j.package)}</span></div>` : ""}
      <ul class="layers">${j.layers.map((l) => `<li class="${l.status === "ok" ? "" : "bad"}">
        <b>${esc(l.name)}</b> — ${l.status === "ok"
          ? `${l.res_m} m/px, ${l.width}×${l.height}, ${l.coverage_pct ?? "?"}% covered · ${l.files.map(esc).slice(0, 4).join(", ")}${l.files.length > 4 ? " …" : ""}`
          : esc(l.message || l.status)}</li>`).join("")}</ul>
    </div>`).join("") : '<p class="muted">No jobs yet — build one from the Build tab.</p>';
  $$("[data-cancel]").forEach((b) => b.addEventListener("click", async () => { await api(`/api/jobs/${b.dataset.cancel}/cancel`, { method: "POST" }); loadJobs(); }));
  $$("[data-del]").forEach((b) => b.addEventListener("click", async () => {
    if (!confirm("Delete this job and its package files?")) return;
    await api(`/api/jobs/${b.dataset.del}`, { method: "DELETE" }); loadJobs();
  }));
  if (active) pollT = setTimeout(loadJobs, 2000);
}

// ----------------------------------------------------------------------------- endpoints
let epMeta = null;
async function loadEndpoints() {
  epMeta = await api("/api/endpoints");
  if (!$("#epType").options.length) {
    $("#epType").innerHTML = Object.entries(epMeta.types).map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join("");
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
  const flat = { ...e }; Object.entries(e.auth || {}).forEach(([k, v]) => (flat["auth." + k] = v));
  $$("[name]", f).forEach((el) => { if (flat[el.name] != null) el.value = flat[el.name]; });
  $("#epTitle").textContent = e.id ? "Edit endpoint" : "Add endpoint";
  syncAuth();
}
function readForm() {
  const o = { auth: {} };
  $$("#epForm [name]").forEach((el) => {
    const v = el.value.trim(); if (v === "") return;
    if (el.name.startsWith("auth.")) o.auth[el.name.slice(5)] = v; else o[el.name] = el.type === "number" ? +v : v;
  });
  return o;
}
function syncAuth() { const t = $("#authType").value; $$("[data-auth]").forEach((d) => (d.style.display = d.dataset.auth === t ? "block" : "none")); }
$("#authType").addEventListener("change", syncAuth);
$("#epPreset").addEventListener("change", (e) => {
  const p = epMeta.presets[e.target.value]; if (!p) return;
  fillForm({ type: p.type, group: p.group, auth: p.auth, name: "" });
  $("#epHint").textContent = p.hint;
});
$("#epReset").addEventListener("click", () => { $("#epForm").reset(); $("#epHint").textContent = ""; $("#epTitle").textContent = "Add endpoint"; syncAuth(); });
$("#epTest").addEventListener("click", async () => {
  $("#epResult").textContent = "Testing…";
  const t = { ...readForm() }; if (bbox) t.test_bbox = bbox;
  try { const r = await api("/api/endpoints/test", { method: "POST", json: t }); $("#epResult").textContent = (r.ok ? "✔ " : "✖ ") + r.message; }
  catch (e) { $("#epResult").textContent = "✖ " + e.message; }
});
$("#epForm").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  try {
    await api("/api/endpoints", { method: "POST", json: readForm() });
    $("#epResult").textContent = "Saved — it now appears as a layer on the Build tab.";
    loadEndpoints(); loadSources();
  } catch (e) { $("#epResult").textContent = "✖ " + e.message; }
});

// ------------------------------------------------------------------------------- library
async function loadLibrary() {
  const l = await api("/api/library");
  $("#libDirs").innerHTML = l.dirs.map((d) => `<span class="path">${esc(d)}</span>`).join(" ");
  $("#libStatus").textContent = l.scan.message || "";
  $("#libTable tbody").innerHTML = l.products.length ? l.products.map((p) =>
    `<tr><td>${esc(p.name)}</td><td>${esc(p.kind)}</td><td>${p.files}</td></tr>`).join("")
    : '<tr><td colspan="3" class="muted">Nothing indexed yet.</td></tr>';
  if (l.scan.running) setTimeout(loadLibrary, 1500); else if (loadLibrary._wasRunning) loadSources();
  loadLibrary._wasRunning = l.scan.running;
}
$("#rescanBtn").addEventListener("click", async () => { await api("/api/library/rescan", { method: "POST" }); loadLibrary(); });
$("#uploadFile").addEventListener("change", async (e) => {
  const file = e.target.files[0]; if (!file) return;
  const fd = new FormData(); fd.append("file", file);
  $("#libStatus").textContent = `Uploading ${file.name}…`;
  try { await api("/api/library/upload", { method: "POST", body: fd }); loadLibrary(); }
  catch (err) { $("#libStatus").textContent = "Upload failed: " + err.message; }
  e.target.value = "";
});

// ---------------------------------------------------------------------------------- boot
loadSources().catch((e) => toast("Could not load sources: " + e.message, 8000));
loadJobs().catch(() => {});
syncAuth();
estimate();
if (location.hash.length > 1) showTab(location.hash.slice(1));
