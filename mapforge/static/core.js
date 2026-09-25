"use strict";
// MapForge UI core: helpers, API client, glossary, shared job-spec state.
// Plain scripts (no build step); later files use the globals defined here.

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const NM = 1852;

// localStorage can throw (private mode, blocked storage) — every access goes through these.
const store = {
  get(k, d = null) { try { const v = localStorage.getItem("mapforge." + k); return v === null ? d : JSON.parse(v); } catch (_) { return d; } },
  set(k, v) { try { localStorage.setItem("mapforge." + k, JSON.stringify(v)); } catch (_) {} },
};

let token = store.get("token", "");
const tokenQuery = () => (token ? "token=" + encodeURIComponent(token) : "");
const withToken = (url) => (token ? url + (url.includes("?") ? "&" : "?") + tokenQuery() : url);

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
  if (!r.ok) {
    const err = new Error(body?.detail || body || r.statusText);
    err.status = r.status;
    throw err;
  }
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

// ------------------------------------------------------------------------------ formatting
const fmtDur = (s) => {
  s = Math.max(0, Math.round(s));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return h ? `${h} h ${m} min` : m ? `${m} min${m < 10 && sec ? ` ${sec} s` : ""}` : `${sec} s`;
};
// "about 3 min" / "under a minute" — plain wording for estimates, not stopwatch precision.
const fmtAbout = (s) => {
  if (s == null || !Number.isFinite(s)) return "unknown time";
  if (s < 5) return "no wait";
  if (s < 60) return "under a minute";
  if (s < 3600) return `about ${Math.round(s / 60)} min`;
  const h = s / 3600;
  return `about ${h < 10 ? h.toFixed(1).replace(/\.0$/, "") : Math.round(h)} h`;
};
const fmtMB = (mb) => {
  if (mb == null || !Number.isFinite(mb)) return "?";
  if (mb < 1) return "< 1 MB";
  if (mb < 1000) return `${Math.round(mb)} MB`;
  return `${(mb / 1000).toFixed(mb < 10000 ? 1 : 0)} GB`;
};
const fmtRes = (m) => (m >= 1 ? `${+m.toFixed(m < 10 ? 1 : 0)} m` : `${Math.round(m * 100)} cm`);

// -------------------------------------------------------------------------------- glossary
// Plain-language explanations for every GIS term the UI shows. gloss("dted") renders a small
// "?" button; clicking it opens the explanation in a popover.
const GLOSSARY = {
  resolution: ["Resolution / detail", "How much ground one pixel covers. 1 m shows cars and buildings, 10 m shows roads and fields, 30 m shows the shape of the terrain. Smaller numbers mean more detail but much bigger downloads."],
  geotiff: ["GeoTIFF", "The standard map-image file: a picture that also records exactly where on Earth it sits. Kongsberg TerraLens and almost every GIS program load it."],
  cog: ["COG (Cloud-Optimized GeoTIFF)", "A GeoTIFF laid out so programs can read just the part they need. Same data, faster over a network. Optional."],
  mbtiles: ["MBTiles", "A single-file package of web-map tiles. Useful for web or phone map viewers; TerraLens normally uses the GeoTIFF instead. Optional."],
  dted: ["DTED (terrain elevation)", "Digital Terrain Elevation Data: the military standard for ground height, stored as 1°×1° files. Level 0 has a height point every ~900 m, Level 1 every ~90 m, Level 2 every ~30 m."],
  epsg4326: ["EPSG:4326 / WGS84", "Plain latitude/longitude, the same coordinate system GPS uses. Kongsberg-ready output uses it so every layer lines up exactly."],
  overviews: ["Overviews", "Pre-shrunk copies stored inside the file so zooming out stays fast."],
  projection: ["Projection", "How the round Earth is flattened onto a flat map. Sources use different projections; the Kongsberg-ready option converts them all to one so they line up."],
  mosaic: ["Mosaic", "Several chart sheets or image tiles stitched into one seamless picture, with chart margins and legends trimmed off."],
  sha256: ["SHA256 checksums", "A fingerprint of every file. After copying the package, run  sha256sum -c SHA256SUMS  (Linux) to prove nothing was corrupted on the way."],
  cache: ["Already downloaded", "Data MapForge fetched for an earlier job is kept and reused, so it costs no download time."],
  coverage: ["Coverage", "Where a source actually has data. Show it on the map to check your area is inside."],
  split: ["Split for media", "Cuts the package zip into numbered pieces that each fit your media (CD, DVD, USB stick with FAT32, Blu-ray). A text file explains how to join them back together."],
};
function gloss(term) {
  if (!GLOSSARY[term]) return "";
  return `<button type="button" class="q" data-gloss="${esc(term)}" aria-label="What is ${esc(GLOSSARY[term][0])}?">?</button>`;
}
document.addEventListener("click", (e) => {
  const b = e.target.closest("[data-gloss]");
  const pop = $("#glossPop");
  if (!b) { if (!e.target.closest("#glossPop")) pop.classList.add("hidden"); return; }
  e.preventDefault(); e.stopPropagation();
  const [title, text] = GLOSSARY[b.dataset.gloss];
  if (!pop.classList.contains("hidden") && pop.dataset.term === b.dataset.gloss) { pop.classList.add("hidden"); return; }
  pop.dataset.term = b.dataset.gloss;
  pop.innerHTML = `<b>${esc(title)}</b><p>${esc(text)}</p>`;
  pop.classList.remove("hidden");
  const r = b.getBoundingClientRect(), w = Math.min(320, window.innerWidth - 24);
  pop.style.width = w + "px";
  pop.style.left = Math.max(12, Math.min(r.left - 20, window.innerWidth - w - 12)) + "px";
  const below = r.bottom + 8, h = pop.offsetHeight;
  pop.style.top = (below + h > window.innerHeight - 8 ? Math.max(8, r.top - h - 8) : below) + "px";
});
document.addEventListener("keydown", (e) => { if (e.key === "Escape") $("#glossPop")?.classList.add("hidden"); });

// ------------------------------------------------------------------------ shared job state
// One job spec, edited by both the guided and the advanced view — switching views keeps it.
const MODES = {
  kongsberg: {
    title: "Ready for Kongsberg", tag: "recommended",
    text: "Cut to your area, stitched into one seamless layer per source, chart margins removed, and converted to latitude/longitude (WGS84) so every layer lines up. Load it straight into TerraLens.",
    get: "One GeoTIFF per layer, plus optional COG / MBTiles / DTED terrain cells.",
    tree: "MyArea/\n  01_faa-sectional/faa-sectional.tif\n  02_usgs-naip/usgs-naip.tif\n  03_copernicus-dem-30/dted/w077/n38.dt1\n  README.txt  manifest.json  SHA256SUMS",
  },
  clipped: {
    title: "Cut to my area, original format",
    text: "Each source file is cut to your box but otherwise left as published: same projection, colours and file type. Nothing is stitched or converted.",
    get: "One file per chart sheet or tile, each in its own projection.",
    tree: "MyArea/\n  01_faa-sectional/Washington SEC (clipped).tif\n  01_faa-sectional/New York SEC (clipped).tif\n  README.txt  (what each file is + its projection)",
  },
  original: {
    title: "Original files, untouched",
    text: "The complete files exactly as the source publishes them (whole charts, whole terrain tiles), with their side files. Largest download; good for archiving or other GIS software.",
    get: "Whole source files and their sidecar files, not cut to your area.",
    tree: "MyArea/\n  01_faa-sectional/Washington SEC.tif  (+ .tfw, .htm)\n  02_copernicus-dem-30/Copernicus_DSM_..._N38_W077.tif\n  README.txt",
  },
};
const MIN_SPLIT_MB = 50;  // the server refuses smaller pieces
const SPLIT_PRESETS = [
  { mb: 700, label: "CD (700 MB)" },
  { mb: 4480, label: "DVD (4.7 GB)" },
  { mb: 4095, label: "USB stick, FAT32 (4 GB)" },
  { mb: 25000, label: "Blu-ray BD-R (25 GB)" },
];

const legacy = store.get("state", {});  // pre-guided-mode saved state
const st = Object.assign({
  bbox: legacy.bbox || null,
  polygon: null,  // [[lon,lat], ...] open ring, or null for a plain box
  mode: "kongsberg",
  layers: legacy.selected || {},  // id -> {res_m: number|""}
  outputs: {
    geotiff: legacy.outputs?.tif ?? true, cog: !!legacy.outputs?.cog, mbtiles: !!legacy.outputs?.mbt,
    dted_level: legacy.outputs?.dted ?? "1",
  },
  delivery: { export: false, exportRoot: "", exportSub: "", split: false, splitMb: 4095 },
  name: legacy.name || "",
  point: { lat: "", lon: "", rad: "10" },
  view: null,   // "guided" | "advanced"; null = first visit -> guided
  step: 0,
  mapView: legacy.view || null,
}, store.get("ui", {}));
st.layers = st.layers || {};
st.point = st.point || { lat: "", lon: "", rad: "10" };
st.delivery = { export: false, exportRoot: "", exportSub: "", split: false, splitMb: 4095, ...(st.delivery || {}) };
function persist() { store.set("ui", st); }

function jobSpec() {
  return {
    name: st.name.trim() || "area",
    bbox: st.bbox,
    polygon: st.polygon,
    mode: st.mode,
    layers: Object.entries(st.layers).map(([source, o]) => ({ source, res_m: o.res_m === "" || o.res_m == null ? null : +o.res_m })),
    outputs: { ...st.outputs, dted_level: st.outputs.dted_level === "" ? null : st.outputs.dted_level },
    // Not part of the processing spec: remembered with the job so the Jobs tab can run the
    // chosen deliveries (save to folder / split) as soon as the package is ready.
    delivery: {
      export: st.delivery.export ? { dest: joinPath(st.delivery.exportRoot, st.delivery.exportSub) } : null,
      split: st.delivery.split ? { part_mb: +st.delivery.splitMb } : null,
    },
  };
}
function joinPath(root, sub) {
  sub = (sub || "").trim().replace(/^\/+/, "");
  return !sub ? root : root.replace(/\/+$/, "") + "/" + sub;
}
