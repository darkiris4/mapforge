"use strict";
// Tabs and boot. Loaded last: core.js → map.js → build.js → jobs.js → admin.js → app.js.

$$("nav button[data-tab]").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));
function showTab(name) {
  if (!$("#tab-" + name)) name = "build";
  if (location.hash.slice(1) !== name) history.replaceState(null, "", "#" + name);
  $$("nav button[data-tab]").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  $$(".tab").forEach((t) => t.classList.toggle("active", t.id === "tab-" + name));
  if (name === "build") setTimeout(() => map.invalidateSize(), 50);
  if (name === "jobs") loadJobs();
  if (name === "endpoints") loadEndpoints().catch((e) => toast(e.message, 6000));
  if (name === "library") loadLibrary().catch((e) => toast(e.message, 6000));
}

renderPanel();
loadSources().catch((e) => { $("#panel").innerHTML = `<p class="bad pad">Could not load data sources: ${esc(e.message)}</p>`; });
loadExportRoots();
loadJobs().catch(() => {});
syncAuth();
if (location.hash.length > 1) showTab(location.hash.slice(1));
window.addEventListener("hashchange", () => showTab(location.hash.slice(1) || "build"));
