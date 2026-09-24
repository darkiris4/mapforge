"use strict";
// Jobs tab: progress, per-layer downloads, deliveries (save to server folder, split for media),
// checksums, run-again, and plain-language failure hints.

let pollT;
const openForm = {};   // job id -> "export" | "split" (inline form kept open across polls)
const formVals = {};   // job id -> {root, sub, splitMb}
let jobsCache = [];

function jobTiming(j) {
  const now = Date.now() / 1000;
  if (j.status === "running" && j.started) {
    const el = now - j.started;
    const eta = j.progress > 0.03 ? (el / j.progress) * (1 - j.progress) : null;
    return `running ${fmtDur(el)}${eta !== null ? ` · about ${fmtDur(eta)} left` : ""}`;
  }
  if (j.finished && j.started) return `took ${fmtDur(j.finished - j.started)}`;
  return "";
}
// Turn raw errors into something a sysadmin can act on; the original text stays available.
function friendlyError(msg) {
  const m = String(msg || "");
  if (/504|gateway time-?out|timed? ?out/i.test(m)) return "The data server was too busy and timed out. Run the job again — pieces already downloaded are reused.";
  if (/5\d\d |server error/i.test(m)) return "The data server returned an error. Try again later; downloaded pieces are reused.";
  if (/pixel limit/i.test(m)) return "Too much detail for an area this size. Choose a lower level of detail or a smaller area.";
  if (/no data .* intersects|intersects the area/i.test(m)) return "This source has no data in your area.";
  if (/ssl|certificate|handshake/i.test(m)) return "The secure connection failed (certificate problem). Check the endpoint's PKI settings on the Endpoints tab.";
  if (/401|403|unauthori[sz]ed|forbidden/i.test(m)) return "The server refused access. Check the endpoint's credentials.";
  if (/resolve|connect|network|unreachable/i.test(m)) return "Couldn't reach the data server. Check this server's internet connection.";
  if (/no space|507|free on/i.test(m)) return "Not enough disk space on the server.";
  return null;
}
const statusWord = { queued: "waiting", running: "running", done: "ready", partial: "partly ready", failed: "failed", cancelled: "cancelled" };

function deliveryHTML_(j) {
  const dv = j.deliveries || [];
  if (!dv.length) return "";
  return `<ul class="dvlist">${dv.map((d) => `<li class="${esc(d.status)}">
    <span class="st ${d.status === "done" ? "done" : d.status === "failed" ? "failed" : ""}">${d.status === "running" ? "working…" : esc(d.status)}</span>
    ${d.type === "export" ? "Copy to server folder" : d.type === "split" ? "Split for media" : esc(d.type)}
    ${d.message ? `<span class="muted"> — ${esc(d.message)}</span>` : ""}
    ${d.type === "export" && d.path ? `<div class="pathrow"><span class="path">${esc(d.path)}</span><button class="small" data-copy="${esc(d.path)}">Copy path</button></div>` : ""}
    ${d.type === "split" && d.status === "done" && d.files?.length ? `<div class="parts">${[...new Set([...d.files, "JOIN-README.txt", "SHA256SUMS"])].map((f) =>
      `<a class="btn small ${/\.txt$|SHA256SUMS/.test(f) ? "" : "primary"}" href="${esc(withToken(`/api/jobs/${encodeURIComponent(j.id)}/parts/${encodeURIComponent(f)}`))}" download>${esc(f)}</a>`).join("")}</div>
      ${d.part_mb ? `<div class="muted">${esc(d.files.filter((f) => /\.\d{3}$/.test(f)).length)} piece(s) of up to ${fmtMB(d.part_mb)}</div>` : ""}
      ${d.path ? `<div class="pathrow"><span class="muted">On server:</span><span class="path">${esc(d.path)}</span><button class="small" data-copy="${esc(d.path)}">Copy path</button></div>` : ""}` : ""}
  </li>`).join("")}</ul>`;
}
function deliverForms(j) {
  const f = openForm[j.id], v = formVals[j.id] || (formVals[j.id] = { root: exportRoots?.default || exportRoots?.roots?.[0] || "", sub: "", splitMb: 4095 });
  const canExport = exportRoots !== false;
  return `<div class="dvbtns row">
      ${canExport ? `<button class="small ${f === "export" ? "on" : ""}" data-form="export" data-j="${esc(j.id)}">Save to server folder…</button>` : ""}
      <button class="small ${f === "split" ? "on" : ""}" data-form="split" data-j="${esc(j.id)}">Split for media…</button>
      <button class="small" data-rerun="${esc(j.id)}" title="Load these settings on the Build tab">Run again / edit…</button>
    </div>
    ${f === "export" && canExport ? `<div class="dvform">
      <label>Folder <select data-fv="root" data-j="${esc(j.id)}">${(exportRoots?.roots || []).map((r) => `<option ${r === v.root ? "selected" : ""}>${esc(r)}</option>`).join("")}</select></label>
      <label>Subfolder (optional)<input data-fv="sub" data-j="${esc(j.id)}" value="${esc(v.sub)}" placeholder="e.g. mission-42"></label>
      <p class="muted">Copies the package to <span class="path">${esc(joinPath(v.root, v.sub))}/${esc(j.name)}</span></p>
      <button class="primary small" data-doexport="${esc(j.id)}">Copy now</button></div>` : ""}
    ${f === "split" ? `<div class="dvform">
      <label>Piece size <select data-fv="preset" data-j="${esc(j.id)}">${SPLIT_PRESETS.map((p) => `<option value="${p.mb}" ${+v.splitMb === p.mb ? "selected" : ""}>${esc(p.label)}</option>`).join("")}
        <option value="custom" ${SPLIT_PRESETS.some((p) => p.mb === +v.splitMb) ? "" : "selected"}>Custom size…</option></select></label>
      <label class="${SPLIT_PRESETS.some((p) => p.mb === +v.splitMb) ? "hidden" : ""}">Custom piece size (MB, at least ${MIN_SPLIT_MB})<input type="number" min="${MIN_SPLIT_MB}" data-fv="splitMb" data-j="${esc(j.id)}" value="${esc(v.splitMb)}"></label>
      <p class="muted">Makes numbered pieces plus a JOIN-README.txt that explains how to put them back together on Linux or Windows. ${gloss("split")}</p>
      <button class="primary small" data-dosplit="${esc(j.id)}">Split now</button></div>` : ""}`;
}
function jobCardHTML(j) {
  const running = ["running", "queued"].includes(j.status);
  const ready = ["done", "partial"].includes(j.status);
  const cur = j.current;
  const mode = MODES[j.spec?.mode || "kongsberg"];
  const zip = withToken(`/api/jobs/${encodeURIComponent(j.id)}/download`);
  const fe = j.status === "failed" ? friendlyError(j.message) : null;
  return `
    <div class="head">
      <div><span class="title">${esc(j.name)}</span> <span class="st ${esc(j.status)}">${esc(statusWord[j.status] || j.status)}</span>
        <span class="tag">${esc(mode?.title || j.spec?.mode)}</span>
        <span class="muted">${new Date(j.created * 1000).toLocaleString()} ${esc(jobTiming(j))}</span></div>
      <div class="row">
        ${ready ? `<a class="btn primary small" href="${esc(zip)}">Download .zip</a>` : ""}
        ${running ? `<button class="small" data-cancel="${esc(j.id)}">Cancel</button>`
          : `${!ready ? `<button class="small" data-rerun="${esc(j.id)}">Run again / edit…</button>` : ""}<button class="small" data-del="${esc(j.id)}">Delete</button>`}
      </div>
    </div>
    <div class="muted">${esc(j.message)}</div>
    ${fe ? `<div class="hint">${esc(fe)}</div>` : ""}
    ${running ? `<div class="bar" title="overall"><div style="width:${(j.progress * 100).toFixed(1)}%"></div></div>
      ${cur ? `<div class="muted">Layer ${cur.index + 1} of ${cur.count}: ${esc(cur.name)}</div>
        <div class="bar thin" title="current layer"><div style="width:${((j.layer_progress || 0) * 100).toFixed(1)}%"></div></div>` : ""}` : ""}
    <ul class="layers">${(j.layers || []).map((l) => {
      const ok = l.status === "ok";
      const fh = !ok && l.status === "failed" ? friendlyError(l.message) : null;
      return `<li class="${ok ? "" : "bad"}"><b>${esc(l.name)}</b> — ${ok
        ? `${l.res_m != null ? `${fmtRes(l.res_m)} detail, ` : ""}${l.coverage_pct != null ? `${l.coverage_pct}% of the area covered, ` : ""}${l.files.length} file${l.files.length === 1 ? "" : "s"}
           ${ready && l.folder ? ` · <a href="${esc(withToken(`/api/jobs/${encodeURIComponent(j.id)}/download?layer=${encodeURIComponent(l.folder)}`))}">download this layer</a>` : ""}`
        : `${esc(fh || l.message || l.status)}${fh ? `<details><summary>Details</summary><code>${esc(l.message)}</code></details>` : ""}`}</li>`;
    }).join("")}</ul>
    ${j.package ? `<div class="muted pathrow">On server: <span class="path">${esc(j.package)}</span><button class="small" data-copy="${esc(j.package)}">Copy path</button></div>` : ""}
    ${ready ? `<div class="deliver-j">
      <div class="muted">Includes SHA256SUMS ${gloss("sha256")} — after copying, check it with
        <code>sha256sum -c SHA256SUMS</code> <button class="small" data-copy="sha256sum -c SHA256SUMS">Copy command</button></div>
      ${deliveryHTML_(j)}${deliverForms(j)}</div>` : ""}`;
}
const jobSig = (j) => JSON.stringify([j.status, j.progress, j.layer_progress, j.message, j.layers?.length, j.deliveries, openForm[j.id], j.current]);

async function loadJobs() {
  clearTimeout(pollT);
  let jobs;
  try { jobs = await api("/api/jobs"); } catch (e) { $("#jobs").innerHTML = `<p class="bad">${esc(e.message)}</p>`; return; }
  jobsCache = jobs;
  const active = jobs.filter((j) => j.status === "running" || j.status === "queued").length;
  $("#jobBadge").textContent = active; $("#jobBadge").classList.toggle("hidden", !active);
  const box = $("#jobs");
  if (!jobs.length) { box.innerHTML = '<p class="muted">No jobs yet — build one from the Build tab.</p>'; }
  else {
    box.querySelector(":scope > p")?.remove();
    const want = new Set(jobs.map((j) => j.id));
    $$(".job", box).forEach((el) => { if (!want.has(el.dataset.id)) el.remove(); });
    let prev = null;
    for (const j of jobs) {
      let el = box.querySelector(`.job[data-id="${CSS.escape(j.id)}"]`);
      if (!el) { el = document.createElement("div"); el.className = "job"; el.dataset.id = j.id; }
      const sig = jobSig(j);
      if (el.dataset.sig !== sig) { el.innerHTML = jobCardHTML(j); el.dataset.sig = sig; wireJob(el, j); }
      if (prev ? prev.nextSibling !== el : box.firstChild !== el) box.insertBefore(el, prev ? prev.nextSibling : box.firstChild);
      prev = el;
    }
  }
  autoDeliver(jobs);
  const busy = active || jobs.some((j) => (j.deliveries || []).some((d) => d.status === "running"));
  if (busy) pollT = setTimeout(loadJobs, 2000);
}
function wireJob(el, j) {
  el.querySelector("[data-cancel]")?.addEventListener("click", async (e) => {
    e.target.disabled = true; await api(`/api/jobs/${encodeURIComponent(j.id)}/cancel`, { method: "POST" }).catch((x) => toast(x.message)); loadJobs();
  });
  el.querySelector("[data-del]")?.addEventListener("click", async () => {
    if (!confirm(`Delete “${j.name}” and its package files from the server?`)) return;
    await api(`/api/jobs/${encodeURIComponent(j.id)}`, { method: "DELETE" }).catch((x) => toast(x.message, 5000)); loadJobs();
  });
  el.querySelectorAll("[data-rerun]").forEach((b) => b.addEventListener("click", () => {
    loadSpec(j.spec); showTab("build"); toast("Settings loaded — review them and press Build");
  }));
  el.querySelectorAll("[data-copy]").forEach((b) => b.addEventListener("click", () => copyText(b.dataset.copy)));
  el.querySelectorAll("[data-form]").forEach((b) => b.addEventListener("click", () => {
    openForm[j.id] = openForm[j.id] === b.dataset.form ? null : b.dataset.form; loadJobs();
  }));
  el.querySelectorAll("[data-fv]").forEach((inp) => inp.addEventListener(inp.tagName === "SELECT" ? "change" : "input", () => {
    const v = formVals[j.id];
    if (inp.dataset.fv === "preset") { if (inp.value !== "custom") v.splitMb = +inp.value; else v.splitMb = ""; el.dataset.sig = ""; loadJobs(); return; }
    v[inp.dataset.fv] = inp.value;
    const p = inp.closest(".dvform")?.querySelector(".path");
    if (p && inp.dataset.fv !== "splitMb") p.textContent = `${joinPath(v.root, v.sub)}/${j.name}`;
  }));
  el.querySelector("[data-doexport]")?.addEventListener("click", () => startDelivery(j, "export", { dest: joinPath(formVals[j.id].root, formVals[j.id].sub) }));
  el.querySelector("[data-dosplit]")?.addEventListener("click", () => {
    const mb = +formVals[j.id].splitMb;
    if (!(mb >= MIN_SPLIT_MB)) return toast(`Enter a piece size of at least ${MIN_SPLIT_MB} MB.`);
    startDelivery(j, "split", { part_mb: mb });
  });
}
async function startDelivery(j, type, body) {
  try {
    await api(`/api/jobs/${encodeURIComponent(j.id)}/${type}`, { method: "POST", json: body });
  } catch (e) {
    // Export target already exists and isn't empty: ask before replacing it.
    if (e.status === 409 && type === "export" && !body.overwrite
        && confirm(`${e.message}\n\nReplace what's in ${joinPath(body.dest, j.name)} with this package?`)) {
      return startDelivery(j, type, { ...body, overwrite: true });
    }
    toast((e.status === 404 || e.status === 405 ? "This server version can't do that yet." : e.message), 8000);
    return false;
  }
  openForm[j.id] = null;
  toast(type === "export" ? "Copying to the server folder…" : "Splitting the package…");
  loadJobs();
  return true;
}
// Deliveries chosen before building (spec.delivery) run once, as soon as the package is ready.
function autoDeliver(jobs) {
  for (const j of jobs) {
    if (!["done", "partial"].includes(j.status) || !j.spec?.delivery) continue;
    for (const [type, body] of Object.entries(j.spec.delivery)) {
      if (!body || !["export", "split"].includes(type)) continue;
      const key = `autodv.${j.id}.${type}`;
      if (store.get(key) || (j.deliveries || []).some((d) => d.type === type)) continue;
      store.set(key, true);
      startDelivery(j, type, body);
    }
  }
}
