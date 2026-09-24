"use strict";
// Endpoints / NGA tab and Local library tab.

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
  const t = { ...readForm() }; if (st.bbox) t.test_bbox = st.bbox;
  try { const r = await api("/api/endpoints/test", { method: "POST", json: t }); $("#epResult").innerHTML = `<span class="${r.ok ? "good" : "bad"}">${r.ok ? "✔" : "✖"} ${esc(r.message)}</span>`; }
  catch (e) { $("#epResult").innerHTML = `<span class="bad">✖ ${esc(e.message)}</span>`; }
});
$("#epForm").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  try {
    const r = await api("/api/endpoints", { method: "POST", json: readForm() });
    $("[name=id]").value = r.id;
    $("#epTitle").textContent = "Edit endpoint";
    $("#epResult").innerHTML = '<span class="good">Saved — it now appears under “Connected services” on the Build tab.</span>';
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
  const status = $("#libStatus"), bar = $("#uploadBar");
  const xhr = new XMLHttpRequest();
  // Raw streaming PUT: the server writes straight into the library (no /tmp copy).
  xhr.open("PUT", "/api/library/upload?filename=" + encodeURIComponent(file.name));
  xhr.setRequestHeader("Content-Type", "application/octet-stream");
  if (token) xhr.setRequestHeader("X-MapForge-Token", token);
  status.dataset.uploading = "1"; bar.classList.remove("hidden");
  xhr.upload.onprogress = (ev) => {
    if (!ev.lengthComputable) return;
    const pct = (ev.loaded / ev.total) * 100;
    $("div", bar).style.width = pct.toFixed(1) + "%";
    status.textContent = pct < 100 ? `Uploading ${file.name}… ${pct.toFixed(0)}%` : `Extracting ${file.name}…`;
  };
  xhr.onloadend = () => {
    delete status.dataset.uploading; bar.classList.add("hidden"); e.target.value = "";
    if (xhr.status >= 200 && xhr.status < 300) { status.textContent = `Uploaded ${file.name}; scanning…`; libWasRunning = true; loadLibrary(); }
    else { let m = xhr.statusText || "network error"; try { m = JSON.parse(xhr.responseText).detail; } catch (_) {} status.textContent = "Upload failed: " + m; }
  };
  xhr.send(file);
});
