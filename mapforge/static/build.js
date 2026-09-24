"use strict";
// Build tab: sources, plain-language detail levels, live size/time estimates, and the two views
// (guided wizard / advanced panel) over the shared job spec in core.js.

// ------------------------------------------------------------------------------ catalogue
const CATEGORIES = [
  { id: "vfr", title: "VFR aeronautical charts", text: "Sectional, terminal-area and helicopter charts, as flown under visual flight rules." },
  { id: "ifr", title: "IFR enroute charts", text: "Instrument-flight route charts: airways, navaids and altitudes." },
  { id: "imagery", title: "Satellite & aerial imagery", text: "Photos of the ground, from 10 m satellite mosaics down to sub-metre aerial photography." },
  { id: "elevation", title: "Terrain elevation", text: "Ground height for 3-D terrain, line-of-sight and DTED." },
  { id: "local", title: "Files on this server (incl. NGA)", text: "Products in the server's library folder: CADRG, CIB, DTED, NITF, GeoTIFF… Add them on the Local library tab." },
  { id: "custom", title: "Connected services", text: "Services added on the Endpoints / NGA tab, e.g. GEGD with a PKI certificate." },
];
const CONUS = [-125, 24, -66.5, 49.5];
// What gets ticked when a whole category is chosen in guided mode.
function recommendedFor(cat) {
  const inConus = st.bbox && st.bbox[0] >= CONUS[0] && st.bbox[2] <= CONUS[2] && st.bbox[1] >= CONUS[1] && st.bbox[3] <= CONUS[3];
  const pick = { vfr: ["faa-sectional"], ifr: ["faa-ifr-low"], imagery: [inConus ? "usgs-naip" : "s2cloudless-2024"], elevation: ["copernicus-dem-30"] }[cat] || [];
  const ok = pick.filter((id) => srcById(id));
  if (ok.length) return ok;
  const first = sources.find((s) => categoryOf(s) === cat);
  return first ? [first.id] : [];
}

let sources = [];
const srcById = (id) => sources.find((s) => s.id === id);
function categoryOf(s) {
  if (s.category) return s.category;
  if (s.access === "local") return "local";
  if (/^ep-/.test(s.id) || /custom|nga|controlled/i.test(s.group || "")) return "custom";
  if (s.id.startsWith("faa-ifr")) return "ifr";
  if (s.id.startsWith("faa-")) return "vfr";
  if (s.kind === "elevation") return "elevation";
  return "imagery";
}
const plainName = (s) => s.plain_name || s.name;
const explainOf = (s) => s.explain || s.description || "";
const isChart = (s) => ["vfr", "ifr"].includes(categoryOf(s)) || s.resampling === "nearest";

function levelLabel(res, s) {
  if (s.kind === "elevation") {
    if (res >= 60) return ["standard", "Standard", "height every ~90 m, like DTED Level 1"];
    if (res >= 20) return ["detailed", "Detailed", "height every ~30 m, like DTED Level 2"];
    if (res >= 5) return ["high", "High", "height every ~10 m (US only)"];
    return ["max", "Very high", "lidar-grade (US only); large"];
  }
  if (res >= 20) return ["overview", "Overview", "coastlines, towns, major roads"];
  if (res >= 8) return ["regional", "Regional", "roads, fields, large buildings"];
  if (res >= 3) return ["area", "Area", "streets and buildings"];
  if (res >= 1.5) return ["detailed", "Detailed", "individual buildings"];
  if (res >= 0.9) return ["verydetailed", "Very detailed", "vehicles visible; big downloads"];
  return ["max", "Maximum", "finest available; very big downloads"];
}
// Detail choices in plain words. Prefer the server's list; derive one for older servers.
function levelsFor(s) {
  if (Array.isArray(s.detail_levels) && s.detail_levels.length) return s.detail_levels;
  if (isChart(s)) return [{ id: "native", label: "As published", res_m: null, hint: "the chart's own scale (recommended)" }];
  const min = (s.min_res_m || 0) * 0.999;
  // Imagery: fixed steps plus the source's finest resolution as "Maximum".
  const cands = s.kind === "elevation" ? [90, 30, 10, 1] : [30, 10, 4, 2, 1, ...(s.min_res_m && s.min_res_m < 0.9 ? [s.min_res_m] : [])];
  let picks = cands.filter((r) => r >= min);
  if (s.default_res_m && !picks.some((r) => Math.abs(r - s.default_res_m) < 1e-6)) picks.push(s.default_res_m);
  picks = [...new Set(picks)].sort((a, b) => b - a);
  const seen = new Set();
  return picks.map((r) => {
    const [id, label, hint] = levelLabel(r, s);
    return { id, label, res_m: r, hint };
  }).filter((l) => (seen.has(l.id) ? false : seen.add(l.id)));
}
// The level currently chosen for a selected layer (res "" = the source's default).
function currentLevel(s) {
  const lv = levelsFor(s), want = st.layers[s.id]?.res_m;
  const eff = want === "" || want == null ? (lv.find((l) => l.res_m == null) ? null : s.default_res_m) : +want;
  const exact = lv.find((l) => (l.res_m == null ? eff == null : eff != null && Math.abs(l.res_m - eff) < 1e-6));
  if (exact || want !== "" && want != null) return exact || null;
  // Left at the default but the server's list has no exact match: show the nearest level.
  const withRes = lv.filter((l) => l.res_m != null);
  return eff == null || !withRes.length ? lv[0] || null
    : withRes.reduce((a, b) => (Math.abs(Math.log(b.res_m / eff)) < Math.abs(Math.log(a.res_m / eff)) ? b : a));
}

async function loadSources() {
  sources = await api("/api/sources");
  for (const id of Object.keys(st.layers)) if (!srcById(id)) delete st.layers[id];
  prefetchCoverage(sources);
  renderPanel();
  scheduleEstimate();
}

// ------------------------------------------------------------------------------ estimate
let est = null, estErr = null, estSeq = 0, estT, estLoading = false;
const layerCount = () => Object.keys(st.layers).length;
function scheduleEstimate() { clearTimeout(estT); estLoading = !!(st.bbox && layerCount()); paintEstimates(); estT = setTimeout(runEstimate, 350); }
async function runEstimate() {
  if (!st.bbox || !layerCount()) { est = null; estErr = null; estLoading = false; paintEstimates(); return; }
  const seq = ++estSeq;
  try {
    const e = await api("/api/estimate", { method: "POST", json: jobSpec() });
    if (seq !== estSeq) return;
    est = e; estErr = null;
  } catch (err) {
    if (seq !== estSeq) return;
    est = null; estErr = err.message;
  }
  estLoading = false;
  paintEstimates();
}
const rowFor = (id) => est?.layers?.find((l) => l.source === id) || null;
// Notes that repeat on every row (e.g. "time is a rough guess…") are shown once, under the totals.
const isGeneralNote = (n) => /rough guess|measured this server/i.test(n);
const layerNotes = (r) => (r.notes || []).filter((n) => !isGeneralNote(n));
const rowSlow = (r) => (r.download_seconds ?? (r.fetch_minutes != null ? r.fetch_minutes * 60 : 0)) >= 1800;
function downloadText(r) {
  if (r.download_mb != null) {
    if (r.cached_pct >= 99) return `<span class="good">Already downloaded — no wait</span>`;
    return `Download <b>${fmtMB(r.download_mb)}</b>, ${fmtAbout(r.download_seconds)}`
      + (r.cached_pct > 0 ? ` <span class="muted">(${Math.round(r.cached_pct)}% already here)</span>` : "");
  }
  if (r.fetch_minutes != null) return `Download: ${r.requests.toLocaleString()} server requests, ${fmtAbout(r.fetch_minutes * 60)}`;
  return `<span class="muted">Download size not known yet</span>`;
}
function layerEstHTML(id) {
  const r = rowFor(id);
  if (!r) return estLoading ? '<span class="muted">Working out size…</span>' : "";
  const pkg = r.package_mb ?? r.est_mb;
  let h = `<div>${downloadText(r)} · in package <b>${fmtMB(pkg)}</b>`
    + (r.speed_basis === "default" ? ` <span class="muted" title="Based on typical speeds; MapForge learns this server's real speed after the first download">(rough)</span>` : "") + `</div>`;
  if (r.too_big) h += `<div class="bad">Too much detail for an area this size — choose a lower level or a smaller area.</div>`;
  else if (rowSlow(r)) h += `<div class="hint">Slow: this will take a long time. A lower level of detail or a smaller area is much faster.</div>`;
  for (const n of layerNotes(r)) h += `<div class="muted">${esc(n)}</div>`;
  const src = srcById(id);
  if (src && hasDataHere(id) === false) h += `<div class="hint">No data from this source in your area.</div>`;
  return h;
}
function totals() {
  if (!est) return null;
  const dl = est.total_download_mb ?? null;
  const secs = est.total_download_seconds ?? (est.layers.some((l) => l.fetch_minutes != null) ? est.layers.reduce((a, l) => a + (l.fetch_minutes || 0) * 60, 0) : null);
  return { dl, secs, pkg: est.total_package_mb ?? est.total_mb };
}
function totalsHTML() {
  const t = totals();
  if (!t) return "";
  return `<div class="totals">
    <div><span class="muted">Download</span><b>${t.dl != null ? fmtMB(t.dl) : "?"}</b></div>
    <div><span class="muted">Time to download</span><b>${fmtAbout(t.secs)}</b></div>
    <div><span class="muted">Package size</span><b>${fmtMB(t.pkg)}</b></div></div>`
    + [...new Set(est.layers.flatMap((l) => (l.notes || []).filter(isGeneralNote)))].map((n) => `<div class="muted">${esc(n)}</div>`).join("");
}
// Why the Build button can't be pressed yet — shown right next to it.
function blockers() {
  const out = [];
  if (!st.bbox) out.push("Choose an area first.");
  if (!layerCount()) out.push("Pick at least one layer.");
  if (st.mode === "kongsberg" && !(st.outputs.geotiff || st.outputs.cog || st.outputs.mbtiles || (hasElevation() && st.outputs.dted_level)))
    out.push("Choose at least one file type to produce.");
  if (st.delivery.export && !st.delivery.exportRoot) out.push("Pick a server folder, or untick “copy to a server folder”.");
  if (st.delivery.split && !(+st.delivery.splitMb >= MIN_SPLIT_MB)) out.push(`Pieces must be at least ${MIN_SPLIT_MB} MB.`);
  if (estErr) out.push(estErr);
  if (est?.layers?.some((l) => l.too_big)) out.push("One layer has too much detail for this area.");
  return out;
}
function warnings() {
  const w = [];
  const t = totals();
  if (t?.secs >= 1800) w.push(`Downloading will take ${fmtAbout(t.secs)}.`);
  if (t?.pkg >= 4000) w.push(`The package will be large (${fmtMB(t.pkg)}).`);
  const none = Object.keys(st.layers).filter((id) => hasDataHere(id) === false).map((id) => plainName(srcById(id) || { name: id }));
  if (none.length) w.push(`No data in your area from: ${none.join(", ")}.`);
  return w;
}
const hasElevation = () => Object.keys(st.layers).some((id) => srcById(id)?.kind === "elevation");
function paintEstimates() {
  $$("[data-est]").forEach((el) => (el.innerHTML = layerEstHTML(el.dataset.est)));
  $$("[data-totals]").forEach((el) => (el.innerHTML = totalsHTML()));
  const b = blockers(), w = warnings();
  $$("[data-why]").forEach((el) => {
    el.innerHTML = b.length ? b.map((x) => `<div class="bad">${esc(x)}</div>`).join("")
      : w.map((x) => `<div class="hint">${esc(x)}</div>`).join("") + (estLoading ? '<div class="muted">Updating estimate…</div>' : "");
  });
  $$("[data-build]").forEach((el) => (el.disabled = b.length > 0 || estLoading));
  $$(".src-pick[data-src]").forEach((el) => el.classList.toggle("nodata", hasDataHere(el.dataset.src) === false));
}
coverageListeners.push(paintEstimates);

// ---------------------------------------------------------------------------------- build
async function build() {
  const b = blockers();
  if (b.length) return toast(b[0], 5000);
  const t = totals(), w = warnings();
  if (t && (t.secs >= 1800 || t.pkg >= 4000 || w.length)) {
    const msg = ["Before you start:", ...w, "", `Download ${t.dl != null ? fmtMB(t.dl) : "size unknown"} (${fmtAbout(t.secs)}), package ${fmtMB(t.pkg)}.`, "", "Start the job?"].join("\n");
    if (!confirm(msg)) return;
  }
  $$("[data-build]").forEach((el) => (el.disabled = true));
  try {
    const j = await api("/api/jobs", { method: "POST", json: jobSpec() });
    toast(`Job “${j.name}” started — follow it on the Jobs tab`);
    showTab("jobs");
  } catch (e) { toast(e.message, 7000); }
  paintEstimates();
}

// Put a job's settings back into the form (Jobs tab "Run again / Edit & re-run").
function loadSpec(spec) {
  st.bbox = null;
  st.layers = Object.fromEntries((spec.layers || []).map((l) => [l.source, { res_m: l.res_m ?? "" }]));
  st.mode = spec.mode || "kongsberg";
  if (spec.outputs) st.outputs = { ...st.outputs, ...spec.outputs, dted_level: spec.outputs.dted_level ?? "" };
  st.name = spec.name || "";
  const d = spec.delivery || {};
  st.delivery.export = !!d.export; st.delivery.split = !!d.split;
  if (d.split?.part_mb) st.delivery.splitMb = d.split.part_mb;
  if (d.export?.dest && exportRoots) {
    const root = exportRoots.roots.find((r) => d.export.dest === r || d.export.dest.startsWith(r.replace(/\/+$/, "") + "/"));
    if (root) { st.delivery.exportRoot = root; st.delivery.exportSub = d.export.dest.slice(root.length).replace(/^\/+/, ""); }
  }
  setBBox(spec.bbox, { fit: true, quiet: true });
  st.step = STEPS.length - 1;  // guided: straight to Review
  persist(); renderPanel(); scheduleEstimate();
}

// ---------------------------------------------------------------------- shared fragments
function srcTags(s) {
  return (s.access === "pki" ? '<span class="tag pki">PKI</span>' : s.access === "local" ? '<span class="tag local">on server</span>' : "")
    + '<span class="tag nodata-tag">no data here</span>';
}
function coverageBtn(s) {
  return s.coverage ? `<button type="button" class="linkbtn cov ${coverageLayers[s.id] ? "on" : ""}" data-cov="${esc(s.id)}" style="--c:${colorFor(s.id)}">
    ${coverageLayers[s.id] ? "Hide" : "Show"} where data exists</button>` : "";
}
function levelControl(s, style = "select") {
  const lv = levelsFor(s), cur = currentLevel(s), res = st.layers[s.id]?.res_m ?? "";
  if (lv.length === 1 && lv[0].res_m == null) return `<div class="muted">${esc(lv[0].label)} — ${esc(lv[0].hint || "")}</div>`;
  if (style === "chips") {
    return `<div class="chips" role="radiogroup" aria-label="Detail for ${esc(plainName(s))}">${lv.map((l) => `
      <button type="button" class="chip ${cur && cur.id === l.id && (cur.res_m === l.res_m) ? "on" : ""}" role="radio"
        aria-checked="${cur && cur.res_m === l.res_m ? "true" : "false"}" data-level="${esc(s.id)}" data-res="${l.res_m ?? ""}">
        <b>${esc(l.label)}</b><span>${l.res_m != null ? fmtRes(l.res_m) : ""}</span><small>${esc(l.hint || "")}</small></button>`).join("")}
    </div>`;
  }
  const custom = !cur && res !== "";
  return `<label>Detail ${gloss("resolution")}
    <select data-levelsel="${esc(s.id)}">${lv.map((l) => `<option value="${l.res_m ?? ""}" ${!custom && cur && cur.res_m === l.res_m ? "selected" : ""}>
      ${esc(l.label)}${l.res_m != null ? ` — ${fmtRes(l.res_m)}` : ""}${l.hint ? ` (${esc(l.hint)})` : ""}</option>`).join("")}
      <option value="custom" ${custom ? "selected" : ""}>Custom…</option></select></label>
    <label class="${custom ? "" : "hidden"}" data-customwrap="${esc(s.id)}">Custom detail, metres per pixel
      <input type="number" step="any" min="${s.min_res_m || 0}" value="${esc(res)}" data-customres="${esc(s.id)}"></label>`;
}
function modeHTML(compact = false) {
  return `<div class="modes" role="radiogroup" aria-label="What to produce">${Object.entries(MODES).map(([k, m]) => `
    <label class="mode ${st.mode === k ? "on" : ""}">
      <input type="radio" name="mode" value="${k}" ${st.mode === k ? "checked" : ""}>
      <div><b>${esc(m.title)}</b>${m.tag ? ` <span class="tag">${esc(m.tag)}</span>` : ""}
        <p>${esc(m.text)}</p>
        ${compact ? "" : `<p class="muted">You get: ${esc(m.get)}</p><details><summary>Example of the folder you'll get</summary><pre>${esc(m.tree)}</pre></details>`}
      </div></label>`).join("")}</div>
    ${st.mode === "kongsberg" ? formatsHTML() : `<p class="muted">Files keep their own format${st.mode === "clipped" ? " and projection" : ""} ${gloss("projection")} — nothing to choose here.</p>`}`;
}
function formatsHTML() {
  return `<fieldset class="formats"><legend>File types</legend>
    <label><input type="checkbox" data-out="geotiff" ${st.outputs.geotiff ? "checked" : ""}> GeoTIFF ${gloss("geotiff")} <span class="muted">— what TerraLens loads (recommended)</span></label>
    <label><input type="checkbox" data-out="cog" ${st.outputs.cog ? "checked" : ""}> Also a Cloud-Optimized GeoTIFF ${gloss("cog")}</label>
    <label><input type="checkbox" data-out="mbtiles" ${st.outputs.mbtiles ? "checked" : ""}> Also MBTiles for web/phone viewers ${gloss("mbtiles")}</label>
    ${hasElevation() ? `<label>Terrain as DTED ${gloss("dted")}
      <select data-dted><option value="" ${!st.outputs.dted_level ? "selected" : ""}>No DTED</option>
        <option value="0" ${st.outputs.dted_level === "0" ? "selected" : ""}>Level 0 — coarse (~900 m)</option>
        <option value="1" ${st.outputs.dted_level === "1" ? "selected" : ""}>Level 1 — standard (~90 m)</option>
        <option value="2" ${st.outputs.dted_level === "2" ? "selected" : ""}>Level 2 — detailed (~30 m)</option></select></label>` : ""}
    <p class="muted">Everything is in latitude/longitude (WGS84) ${gloss("epsg4326")} with built-in zoom levels ${gloss("overviews")}.</p>
  </fieldset>`;
}
let exportRoots;  // undefined = not loaded, false = not supported by this server
async function loadExportRoots() {
  try {
    exportRoots = await api("/api/export-roots");
    if (!exportRoots?.roots?.length) exportRoots = false;
    else if (!exportRoots.roots.includes(st.delivery.exportRoot)) st.delivery.exportRoot = exportRoots.default || exportRoots.roots[0];
  } catch (_) { exportRoots = false; st.delivery.export = false; }
  renderPanel();
}
function deliveryHTML() {
  const d = st.delivery;
  const preset = SPLIT_PRESETS.find((p) => p.mb === +d.splitMb);
  return `<div class="deliver">
    <p><b>Download from this page</b> <span class="muted">— a .zip of the whole package (or single layers) is always available on the Jobs tab when the job finishes.</span></p>
    ${exportRoots === false ? '<p class="muted">Copying to a server folder isn\'t available on this server version.</p>' : `
    <label class="check"><input type="checkbox" data-dv="export" ${d.export ? "checked" : ""}>
      Also copy it to a folder on the server <span class="muted">(e.g. a mounted network share)</span></label>
    <div class="sub ${d.export ? "" : "hidden"}">
      <label>Folder <select data-dv="exportRoot">${(exportRoots?.roots || []).map((r) => `<option ${r === d.exportRoot ? "selected" : ""}>${esc(r)}</option>`).join("")}</select></label>
      <label>Subfolder (optional)<input data-dv="exportSub" value="${esc(d.exportSub)}" placeholder="e.g. mission-42/charts"></label>
      <p class="muted">Will be copied to <span class="path">${esc(joinPath(d.exportRoot || "…", d.exportSub))}/&lt;package name&gt;</span></p>
    </div>`}
    <label class="check"><input type="checkbox" data-dv="split" ${d.split ? "checked" : ""}>
      Also split the zip into pieces for removable media ${gloss("split")}</label>
    <div class="sub ${d.split ? "" : "hidden"}">
      <label>Piece size <select data-dv="splitPreset">${SPLIT_PRESETS.map((p) => `<option value="${p.mb}" ${preset?.mb === p.mb ? "selected" : ""}>${esc(p.label)}</option>`).join("")}
        <option value="custom" ${preset ? "" : "selected"}>Custom size…</option></select></label>
      <label class="${preset ? "hidden" : ""}" data-splitcustom>Custom piece size (MB, at least ${MIN_SPLIT_MB})<input type="number" min="${MIN_SPLIT_MB}" data-dv="splitMb" value="${esc(d.splitMb)}"></label>
    </div>
    <p class="muted">Every package includes a SHA256SUMS file so you can check the copy arrived intact ${gloss("sha256")}.</p>
    <label>Package name<input data-name value="${esc(st.name)}" placeholder="e.g. DCA_training_area" maxlength="60"></label>
  </div>`;
}

// ---------------------------------------------------------------------- panel rendering
const panel = $("#panel");
function renderPanel() {
  if (!sources.length && !panel.dataset.loaded) { panel.innerHTML = '<p class="muted pad">Loading data sources…</p>'; return; }
  panel.dataset.loaded = "1";
  document.body.dataset.view = st.view;
  $$("[data-view]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.view === st.view)));
  if (st.view === "advanced") renderAdvanced(); else renderGuided();
  paintEstimates();
}
function wirePanel() {
  panel.querySelectorAll("[data-cov]").forEach((b) => b.addEventListener("click", () => toggleCoverage(b.dataset.cov, plainName(srcById(b.dataset.cov)))));
  panel.querySelectorAll("[data-levelsel]").forEach((sel) => sel.addEventListener("change", () => {
    const id = sel.dataset.levelsel, wrap = panel.querySelector(`[data-customwrap="${CSS.escape(id)}"]`);
    if (sel.value === "custom") { wrap?.classList.remove("hidden"); wrap?.querySelector("input")?.focus(); return; }
    wrap?.classList.add("hidden");
    st.layers[id].res_m = sel.value; persist(); scheduleEstimate();
  }));
  panel.querySelectorAll("[data-customres]").forEach((inp) => inp.addEventListener("change", () => {
    st.layers[inp.dataset.customres].res_m = inp.value; persist(); scheduleEstimate();
  }));
  panel.querySelectorAll("[data-level]").forEach((b) => b.addEventListener("click", () => {
    st.layers[b.dataset.level].res_m = b.dataset.res; persist(); renderPanel(); scheduleEstimate();
  }));
  panel.querySelectorAll("input[name=mode]").forEach((r) => r.addEventListener("change", () => { st.mode = r.value; persist(); renderPanel(); scheduleEstimate(); }));
  panel.querySelectorAll("[data-out]").forEach((c) => c.addEventListener("change", () => { st.outputs[c.dataset.out] = c.checked; persist(); scheduleEstimate(); }));
  panel.querySelector("[data-dted]")?.addEventListener("change", (e) => { st.outputs.dted_level = e.target.value; persist(); scheduleEstimate(); });
  panel.querySelectorAll("[data-dv]").forEach((el) => el.addEventListener(el.type === "checkbox" || el.tagName === "SELECT" ? "change" : "input", () => {
    const k = el.dataset.dv, d = st.delivery;
    if (k === "splitPreset") {
      d.splitMb = el.value === "custom" ? "" : +el.value;  // "" shows the custom-size field
      persist(); renderPanel();
      if (el.value === "custom") panel.querySelector("[data-dv=splitMb]")?.focus();
      return;
    }
    d[k] = el.type === "checkbox" ? el.checked : el.value;
    persist();
    if (el.type === "checkbox" || el.tagName === "SELECT") renderPanel(); else paintEstimates();
  }));
  panel.querySelector("[data-name]")?.addEventListener("input", (e) => { st.name = e.target.value; persist(); });
  panel.querySelectorAll("[data-build]").forEach((b) => b.addEventListener("click", build));
  panel.querySelectorAll("[data-goto]").forEach((b) => b.addEventListener("click", () => { st.step = +b.dataset.goto; persist(); renderPanel(); panel.scrollTop = 0; }));
  wireArea();
}
function toggleLayer(id, on) {
  if (on) st.layers[id] = st.layers[id] || { res_m: "" }; else delete st.layers[id];
  persist(); renderPanel(); scheduleEstimate();
}

// Area controls (same markup in both views).
function areaHTML(guided = false) {
  return `<div class="area">
    <div class="row">
      <button type="button" class="${drawing ? "" : "primary"}" data-area="draw">${drawing ? "✕ Stop drawing" : "▭ Draw on the map"}</button>
      <button type="button" data-area="view">Use what's on screen</button>
      ${st.bbox ? `<button type="button" class="danger" data-area="clear" title="Remove the box (Delete key)">Clear box</button>` : ""}
    </div>
    <details ${guided ? "open" : ""} class="pt"><summary>Around a point (lat/lon + radius)</summary>
      <div class="grid3">
        <label>Latitude<input id="cLat" type="number" step="any" placeholder="38.85" data-pt="lat" value="${esc(st.point.lat)}"></label>
        <label>Longitude<input id="cLon" type="number" step="any" placeholder="-77.04" data-pt="lon" value="${esc(st.point.lon)}"></label>
        <label>Radius (NM)<input id="cRad" type="number" step="any" data-pt="rad" value="${esc(st.point.rad)}"></label>
      </div>
      <button type="button" data-area="center">Set box around this point</button>
    </details>
    <details class="pt"><summary>Exact corners</summary>
      <div class="grid4">
        <label>West<input data-corner="0" type="number" step="any" value="${st.bbox?.[0] ?? ""}"></label>
        <label>South<input data-corner="1" type="number" step="any" value="${st.bbox?.[1] ?? ""}"></label>
        <label>East<input data-corner="2" type="number" step="any" value="${st.bbox?.[2] ?? ""}"></label>
        <label>North<input data-corner="3" type="number" step="any" value="${st.bbox?.[3] ?? ""}"></label>
      </div>
      <label>…or paste “west, south, east, north”<input data-paste placeholder="-77.15, 38.80, -76.95, 38.95"></label>
      <p class="muted">Decimal degrees; west and south are negative in the Americas / southern hemisphere.</p>
    </details>
    <div class="areasum">${areaSummaryHTML()}</div>
  </div>`;
}
function wireArea() {
  panel.querySelector("[data-area=draw]")?.addEventListener("click", () => { setDrawing(!drawing); });
  panel.querySelector("[data-area=view]")?.addEventListener("click", () => {
    if (st.bbox && !confirm("Replace the current box with what's on screen?")) return;
    bboxFromView();
  });
  panel.querySelector("[data-area=clear]")?.addEventListener("click", clearBBox);
  // Keep typed point/radius across re-renders (clearing the box re-draws the panel).
  panel.querySelectorAll("[data-pt]").forEach((inp) => inp.addEventListener("input", () => { st.point[inp.dataset.pt] = inp.value; persist(); }));
  panel.querySelector("[data-area=center]")?.addEventListener("click", () =>
    bboxFromCenter(parseFloat($("#cLat").value), parseFloat($("#cLon").value), parseFloat($("#cRad").value)));
  const corners = panel.querySelectorAll("[data-corner]");
  corners.forEach((c) => c.addEventListener("change", () => {
    const v = [...corners].map((x) => x.value);
    if (v.every((x) => x !== "")) setBBox(v, { fit: true }); else c.classList.add("pending");
  }));
  panel.querySelector("[data-paste]")?.addEventListener("change", (e) => {
    const v = e.target.value.split(/[\s,;]+/).filter(Boolean).map(Number);
    if (v.length === 4 && v.every(Number.isFinite)) setBBox(v, { fit: true }); else toast("Paste four numbers: west, south, east, north.");
  });
}
drawListeners.push(() => renderPanel());
onAreaChange(() => { prefetchCoverage(sources); renderPanel(); scheduleEstimate(); });

// ------------------------------------------------------------------------------ advanced
function renderAdvanced() {
  const f = (store.get("filter", "") || "").toLowerCase();
  const matches = (s) => !f || [plainName(s), s.name, explainOf(s), s.group].join(" ").toLowerCase().includes(f);
  const groups = CATEGORIES.map((c) => ({ c, list: sources.filter((s) => categoryOf(s) === c.id && matches(s)) })).filter((g) => g.list.length);
  panel.innerHTML = `
    <div class="card"><h3>1 · Area</h3>${areaHTML()}</div>
    <div class="card"><h3>2 · Layers <span class="muted count">${layerCount() ? `${layerCount()} selected` : ""}</span></h3>
      <div class="row"><input id="srcFilter" placeholder="Filter layers…" value="${esc(f)}">
        ${layerCount() ? '<button type="button" class="small" data-clearlayers>Clear all</button>' : ""}</div>
      ${groups.map(({ c, list }) => `<div class="group"><div class="gh">${esc(c.title)}</div>
        ${list.map((s) => `<div class="src src-pick ${st.layers[s.id] ? "checked" : ""}" data-src="${esc(s.id)}">
          <div class="top"><label><input type="checkbox" data-pick="${esc(s.id)}" ${st.layers[s.id] ? "checked" : ""}>${esc(plainName(s))}</label>${srcTags(s)}</div>
          <div class="desc">${esc(explainOf(s))}${plainName(s) !== s.name ? ` <span class="tech">${esc(s.name)}</span>` : ""}</div>
          ${st.layers[s.id] ? `<div class="opts">${levelControl(s)}<div class="est" data-est="${esc(s.id)}"></div>
            <div class="meta">${coverageBtn(s)}<details class="lic"><summary>Licence</summary><p class="muted">${esc(s.license || "—")}</p></details></div></div>`
            : `<div class="meta">${coverageBtn(s)}</div>`}
        </div>`).join("")}</div>`).join("") || '<p class="muted">No layers match that filter.</p>'}
    </div>
    <div class="card"><h3>3 · What to produce</h3>${modeHTML(true)}</div>
    <div class="card"><h3>4 · Delivery</h3>${deliveryHTML()}</div>
    <div class="card sticky-build"><div data-totals></div><div data-why class="why"></div>
      <button type="button" class="primary big" data-build>Build package</button></div>`;
  $("#srcFilter").addEventListener("input", (e) => {
    store.set("filter", e.target.value);
    const pos = e.target.selectionStart; renderPanel();
    const el = $("#srcFilter"); el.focus(); el.setSelectionRange(pos, pos);
  });
  panel.querySelectorAll("[data-pick]").forEach((c) => c.addEventListener("change", () => toggleLayer(c.dataset.pick, c.checked)));
  panel.querySelector("[data-clearlayers]")?.addEventListener("click", () => { st.layers = {}; persist(); renderPanel(); scheduleEstimate(); });
  wirePanel();
}

// -------------------------------------------------------------------------------- guided
const STEPS = ["Where", "What", "Detail", "Produce", "Deliver", "Review"];
function stepReady(i) {
  if (i === 0) return !!st.bbox;
  if (i === 1) return layerCount() > 0;
  return true;
}
function renderGuided() {
  st.step = Math.max(0, Math.min(st.step || 0, STEPS.length - 1));
  // Can't be past a step whose prerequisite is missing (e.g. area cleared while on Review).
  for (let i = 0; i < st.step; i++) if (!stepReady(i)) { st.step = i; break; }
  const i = st.step;
  const body = [guidedWhere, guidedWhat, guidedDetail, guidedProduce, guidedDeliver, guidedReview][i]();
  panel.innerHTML = `
    <ol class="stepper" aria-label="Steps">${STEPS.map((s, k) => `<li class="${k === i ? "on" : k < i ? "done" : ""}">
      <button type="button" data-goto="${k}" ${k > i && !Array.from({ length: k }, (_, j) => stepReady(j)).every(Boolean) ? "disabled" : ""}
        aria-current="${k === i ? "step" : "false"}"><span>${k + 1}</span>${esc(s)}</button></li>`).join("")}</ol>
    <div class="gstep">${body}</div>
    <div class="gnav">
      ${i > 0 ? `<button type="button" data-goto="${i - 1}">← Back</button>` : "<span></span>"}
      ${i < STEPS.length - 1 ? `<button type="button" class="primary" data-next ${stepReady(i) ? "" : "disabled"}>Next: ${esc(STEPS[i + 1])} →</button>`
        : `<button type="button" class="primary" data-build>Build package</button>`}
    </div>
    ${i < STEPS.length - 1 ? "" : '<div data-why class="why"></div>'}`;
  panel.querySelector("[data-next]")?.addEventListener("click", () => { if (stepReady(i)) { st.step = i + 1; persist(); renderPanel(); panel.scrollTop = 0; } });
  panel.querySelectorAll("[data-cat]").forEach((c) => c.addEventListener("change", () => {
    const cat = c.dataset.cat;
    if (c.checked) recommendedFor(cat).forEach((id) => (st.layers[id] = st.layers[id] || { res_m: "" }));
    else sources.filter((s) => categoryOf(s) === cat).forEach((s) => delete st.layers[s.id]);
    persist(); renderPanel(); scheduleEstimate();
  }));
  panel.querySelectorAll("[data-pick]").forEach((c) => c.addEventListener("change", () => toggleLayer(c.dataset.pick, c.checked)));
  wirePanel();
}
function guidedWhere() {
  return `<h2>Where do you need maps?</h2>
    <p class="lead">Mark the area on the map. Draw a box, or give a point and a radius, e.g. from a tasking.</p>
    ${areaHTML(true)}
    ${drawing ? '<p class="hint">Press and drag on the map to draw. Press Esc to cancel.</p>' : ""}`;
}
function guidedWhat() {
  return `<h2>What do you need?</h2><p class="lead">Tick what you need. We pick a sensible source in each; open a group to change it.</p>
    ${CATEGORIES.map((c) => {
      const list = sources.filter((s) => categoryOf(s) === c.id);
      const on = list.some((s) => st.layers[s.id]);
      return `<div class="catcard ${on ? "on" : ""} ${list.length ? "" : "empty"}">
        <label class="cathead"><input type="checkbox" data-cat="${c.id}" ${on ? "checked" : ""} ${list.length ? "" : "disabled"}>
          <div><b>${esc(c.title)}</b><p>${esc(c.text)}</p></div></label>
        ${!list.length ? `<p class="muted">None available yet${c.id === "local" ? " — add files on the Local library tab" : c.id === "custom" ? " — add one on the Endpoints / NGA tab" : ""}.</p>`
          : `<details ${on ? "open" : ""}><summary>${on ? "Chosen: " + esc(list.filter((s) => st.layers[s.id]).map(plainName).join(", ") || "none") : `${list.length} option${list.length > 1 ? "s" : ""}`}</summary>
            ${list.map((s) => `<div class="src src-pick ${st.layers[s.id] ? "checked" : ""}" data-src="${esc(s.id)}">
              <div class="top"><label><input type="checkbox" data-pick="${esc(s.id)}" ${st.layers[s.id] ? "checked" : ""}>${esc(plainName(s))}</label>${srcTags(s)}</div>
              <div class="desc">${esc(explainOf(s))}</div><div class="meta">${coverageBtn(s)}</div></div>`).join("")}</details>`}
      </div>`;
    }).join("")}`;
}
function guidedDetail() {
  const chosen = Object.keys(st.layers).map(srcById).filter(Boolean);
  return `<h2>How much detail?</h2>
    <p class="lead">More detail means sharper maps but bigger, slower downloads. The size and time update as you choose. ${gloss("resolution")}</p>
    ${chosen.map((s) => `<div class="detailcard"><b>${esc(plainName(s))}</b>
      ${levelControl(s, "chips")}<div class="est" data-est="${esc(s.id)}"></div></div>`).join("")}
    <div data-totals></div>`;
}
function guidedProduce() {
  return `<h2>What should MapForge produce?</h2><p class="lead">Pick how the files should come out. You can change this later.</p>${modeHTML(false)}`;
}
function guidedDeliver() {
  return `<h2>How will you get it?</h2><p class="lead">Choose any extra ways to hand over the package, and give it a name.</p>${deliveryHTML()}`;
}
function guidedReview() {
  const layers = Object.keys(st.layers).map(srcById).filter(Boolean);
  const d = st.delivery, m = MODES[st.mode];
  const fm = st.mode === "kongsberg" ? [st.outputs.geotiff && "GeoTIFF", st.outputs.cog && "COG", st.outputs.mbtiles && "MBTiles",
    hasElevation() && st.outputs.dted_level && `DTED Level ${st.outputs.dted_level}`].filter(Boolean).join(", ") : "";
  return `<h2>Check and build</h2>
    <section class="rev"><div class="rh"><b>Area</b><button type="button" class="linkbtn" data-goto="0">Change</button></div>${areaSummaryHTML()}</section>
    <section class="rev"><div class="rh"><b>Layers</b><button type="button" class="linkbtn" data-goto="1">Change</button>
      <button type="button" class="linkbtn" data-goto="2">Detail</button></div>
      ${layers.map((s) => { const lv = currentLevel(s); return `<div class="revlayer"><div><b>${esc(plainName(s))}</b>
        <span class="muted">${lv ? esc(lv.label) + (lv.res_m != null ? ` · ${fmtRes(lv.res_m)}` : "") : `custom · ${esc(st.layers[s.id].res_m)} m`}</span></div>
        <div class="est" data-est="${esc(s.id)}"></div></div>`; }).join("")}
    </section>
    <section class="rev"><div class="rh"><b>Output</b><button type="button" class="linkbtn" data-goto="3">Change</button></div>
      ${esc(m.title)}${fm ? ` — ${esc(fm)}` : ""}</section>
    <section class="rev"><div class="rh"><b>Delivery</b><button type="button" class="linkbtn" data-goto="4">Change</button></div>
      <div>Download from the Jobs tab${d.export && exportRoots ? `; copy to <span class="path">${esc(joinPath(d.exportRoot, d.exportSub))}</span>` : ""}${d.split ? `; split into ${esc(SPLIT_PRESETS.find((p) => p.mb === +d.splitMb)?.label || d.splitMb + " MB")} pieces` : ""}.</div>
      <div class="muted">Name: ${esc(st.name.trim() || "area")} · SHA256 checksums included</div></section>
    <div data-totals></div>`;
}

// --------------------------------------------------------------------------- view toggle
$$("[data-view]").forEach((b) => b.addEventListener("click", () => {
  st.view = b.dataset.view; persist(); showTab("build"); renderPanel();
}));
if (!st.view) st.view = "guided";
