"use strict";
const $ = (id) => document.getElementById(id);
let authenticated = false;
let sessionRevision = 0;
let configuration = null;
let snapshot = null;
let activeView = "activity";
let polling = false;
const states = {
  waiting: ["Waiting for subtitle", "text-bg-light"], queued: ["Queued", "text-bg-light"],
  retry: ["Retry scheduled", "text-bg-warning"], processing: ["Processing", "text-bg-primary"],
  // Both of these finished successfully; the difference is whether anything was written.
  unchanged: ["Checked · left alone", "text-bg-success"],
  completed: ["Subtitle written", "text-bg-success"], review: ["Needs attention", "text-bg-warning"],
  failed: ["Failed", "text-bg-danger"], superseded: ["Superseded · restarted", "text-bg-light"],
};
function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}
function message(text, error = false) {
  $("message").textContent = text;
  $("message").className = `alert ${error ? "alert-danger" : "alert-success"}`;
  $("message").hidden = !text;
}
async function api(path, method = "GET", data) {
  const response = await fetch(`/api${path}`, {
    method, headers: { "Content-Type": "application/json" },
    body: data === undefined ? undefined : JSON.stringify(data),
  });
  const result = await response.json();
  if (!response.ok) {
    if (response.status === 401) endSession();
    throw new Error(typeof result.detail === "string" ? result.detail : "The request could not be completed.");
  }
  return result;
}
function view(name) {
  activeView = name;
  $("activity-view").hidden = name !== "activity";
  $("settings-view").hidden = name !== "settings";
  document.querySelectorAll(".nav-button").forEach(button => {
    button.classList.toggle("active", button.dataset.view === name);
    if (button.dataset.view === name) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
}
function endSession() {
  authenticated = false;
  sessionRevision += 1;
  snapshot = null;
  $("machine-api-key").value = "";
  $("machine-api-key").type = "password";
  $("toggle-api-key").textContent = "Show key";
  $("toggle-api-key").setAttribute("aria-pressed", "false");
  renderJobs();
  ["activity-view", "settings-view", "navigation", "logout"].forEach(id => $(id).hidden = true);
  $("login-view").hidden = false;
  $("service-state").textContent = "Subtitle automation";
  showLogin();
}
async function signOut() {
  await action($("logout"), async () => {
    const response = await fetch("/api/session", { method: "DELETE" });
    if (!response.ok) throw new Error("Could not sign out. Please try again.");
    endSession();
  });
}
function relativeTime(value) {
  const seconds = Math.round(Date.now() / 1000 - value);
  if (seconds < 60) return "Just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return new Date(value * 1000).toLocaleDateString();
}
const PENDING = ["queued", "waiting", "retry"];
const ORIGINS = { manual: "you asked for it", import: "new import", bazarr: "Bazarr subtitle", retry: "retry", backlog: "library sweep" };
function jobWhy(job) {
  const source = job.report ? (job.report.mode === "authored_timing" ? "checked against audio" : "transcribed") : null;
  return [ORIGINS[job.origin] || "library sweep", job.manager, source].filter(Boolean).join(" · ");
}
let openDetail = null;
const detailHome = () => $("job-detail").closest("#detail-home") ? null : document.getElementById("detail-home");
function parkDetails() {
  // The list re-renders every few seconds; move the panel out first or it is destroyed.
  const home = document.getElementById("detail-home");
  if (home && $("job-detail").parentElement !== home) home.append($("job-detail"));
  $("job-detail").hidden = true;
}
function jobRow(job, tbody) {
  const row = element("tr");
  row.dataset.job = job.id;
  const media = element("td", undefined, "media-cell");
  media.append(element("div", job.title, "media-title"));
  let note = job.state === "processing" ? job.stage : job.error;
  if (job.state === "waiting") note = `Eligible ${new Date(job.ready * 1000).toLocaleString()}`;
  if (note) media.append(element("div", note, "job-note"));
  media.append(element("div", jobWhy(job), "job-note"));

  const status = element("td");
  const [label, color] = states[job.state] || [job.state, "text-bg-light"];
  status.append(element("span", label, `badge ${color}`));

  const actions = element("td", undefined, "text-end text-nowrap");
  const details = element("button", "Details", "btn btn-sm btn-outline-secondary");
  details.addEventListener("click", () => toggleDetails(job, row, details));
  actions.append(details);
  actions.append(jobActions(job));
  if (["failed", "review", "unchanged", "completed"].includes(job.state)) {
    const retry = element("button", "Check again", "btn btn-sm btn-outline-secondary ms-2");
    retry.addEventListener("click", () => action(retry, () => api(`/jobs/${job.id}/retry`, "POST")));
    actions.append(retry);
  }
  row.append(media, status, element("td", relativeTime(job.updated), "text-secondary text-nowrap"), actions);
  tbody.append(row);
}
function jobActions(job) {
  const wrap = element("span", undefined, "text-nowrap");
  const add = (text, cls, path) => {
    const button = element("button", text, `btn btn-sm ${cls} ms-2`);
    button.addEventListener("click", () => action(button, () => api(path, "POST")));
    wrap.append(button);
  };
  if (PENDING.includes(job.state)) add("Process next", "btn-outline-primary", `/jobs/${job.id}/promote`);
  if ([...PENDING, "processing"].includes(job.state) && !job.cancel_requested) add("Cancel", "btn-outline-danger", `/jobs/${job.id}/cancel`);
  return wrap;
}
function renderNow(job) {
  $("now").hidden = !job;
  $("now-idle").hidden = Boolean(job);
  if (!job) return;
  $("now-title").textContent = job.title;
  $("now-stage").textContent = job.stage || "Starting…";
  $("now-actions").replaceChildren(jobActions(job));
}
function renderSummary(counts) {
  const waiting = (counts.queued || 0) + (counts.waiting || 0) + (counts.retry || 0);
  const summary = [["Subtitle written", counts.completed || 0], ["Checked · left alone", counts.unchanged || 0],
    ["Needs attention", (counts.review || 0) + (counts.failed || 0)], ["Waiting", waiting]];
  $("queue-summary").replaceChildren();
  for (const [label, value] of summary) {
    const item = element("span", undefined, "summary-item");
    item.append(element("b", value.toLocaleString()), element("span", label));
    $("queue-summary").append(item);
  }
}
function renderNext(next, waiting) {
  $("next-count").textContent = next.length
    ? (waiting > next.length ? `Next up — showing ${next.length} of ${waiting.toLocaleString()} waiting` : `Next up — ${waiting} waiting`)
    : "Nothing waiting";
  const list = $("next");
  list.replaceChildren();
  for (const job of next) {
    const item = element("li", undefined, "next-item");
    const label = element("span", undefined, "next-label");
    label.append(element("span", job.title, "next-title"), element("span", jobWhy(job), "job-note"));
    item.append(label, jobActions(job));
    list.append(item);
  }
}
function renderJobs() {
  if (!snapshot) { $("results").replaceChildren(); return; }
  parkDetails();
  const counts = snapshot.counts || {};
  const all = snapshot.jobs || [];
  const total = states => states.reduce((sum, state) => sum + (counts[state] || 0), 0);

  renderNow(all.find(job => job.state === "processing"));
  renderSummary(counts);
  const next = all.filter(job => PENDING.includes(job.state));
  renderNext(next, total(PENDING));

  const filter = $("filter").value;
  const finished = all.filter(job => !PENDING.includes(job.state) && job.state !== "processing");
  const shown = finished.filter(job => filter === "all" || job.state === filter ||
    (filter === "attention" && ["review", "failed"].includes(job.state)));
  const done = total(["completed", "unchanged", "review", "failed", "superseded"]);
  const body = $("results");
  body.replaceChildren();
  for (const job of shown) jobRow(job, body);
  $("results-empty").hidden = shown.length > 0;
  $("results-empty").textContent = filter === "all"
    ? "No results yet. Crowbarr will list what it decided here."
    : "No results of that kind yet.";
  $("results-count").textContent = done > finished.length
    ? `showing the ${shown.length} most recent of ${done.toLocaleString()}`
    : `${shown.length} of ${done.toLocaleString()}`;

  if (openDetail) {
    const row = document.querySelector(`tr[data-job="${openDetail}"]`);
    const job = all.find(item => item.id === openDetail);
    if (row && job) openDetails(job, row, row.querySelector("button"));
    else openDetail = null;
  }
}
function closeDetails() {
  openDetail = null;
  parkDetails();
  document.querySelectorAll("tr.detail-row").forEach(node => node.remove());
  document.querySelectorAll(".activity-table button").forEach(node => {
    if (node.textContent === "Hide details") node.textContent = "Details";
  });
}
function openDetails(job, row, button) {
  const holder = element("tr", undefined, "detail-row");
  const cell = element("td"); cell.colSpan = 4;
  holder.append(cell);
  row.after(holder);
  buildDetails(job);
  cell.append($("job-detail"));
  $("job-detail").hidden = false;
  openDetail = job.id;
  if (button) button.textContent = "Hide details";
}
function toggleDetails(job, row, button) {
  const isOpen = openDetail === job.id;
  closeDetails();
  if (!isOpen) openDetails(job, row, button);
}
function buildDetails(job) {
  $("job-detail").hidden = false;
  $("detail-title").textContent = job.title;
  $("detail-path").textContent = job.media;
  $("detail-description").textContent = job.error || (job.state === "completed" ?
    "A separate Crowbarr subtitle was saved. Your original subtitle is unchanged." : job.stage || "Waiting for processing.");
  $("detail-stats").replaceChildren();
  $("detail-issues").replaceChildren();
  $("detail-audit").replaceChildren();
  if (job.report) {
    const report = job.report;
    if (report.candidate) {
      const download = element("button", "Download review subtitle", "btn btn-sm btn-outline-primary mb-3");
      download.addEventListener("click", () => action(download, async () => {
        const response = await fetch(`/api/jobs/${job.id}/candidate`);
        if (!response.ok) throw new Error("Candidate is unavailable");
        const url = URL.createObjectURL(await response.blob());
        const link = element("a"); link.href = url; link.download = `crowbarr-${job.id}.srt`; link.click();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
      }));
      $("detail-audit").append(download);
      if (job.state === "review") {
        const approval = element("div", undefined, "mb-3");
        const check = element("input", undefined, "form-check-input me-2"); check.type = "checkbox"; check.id = "review-confirm";
        const label = element("label", "I reviewed the candidate against the video", "form-check-label"); label.htmlFor = check.id;
        const publish = element("button", "Publish reviewed candidate", "btn btn-sm btn-outline-primary d-block mt-2"); publish.disabled = true;
        check.addEventListener("change", () => publish.disabled = !check.checked);
        publish.addEventListener("click", () => action(publish, () => api(`/jobs/${job.id}/approve`, "POST")));
        approval.append(check,label,publish); $("detail-audit").append(approval);
      }
    }
    if (report.plex_delivery) $("detail-audit").append(element("p", `Plex delivery: ${report.plex_delivery.state}${report.plex_delivery.stream ? " · stream " + report.plex_delivery.stream.id + " · " + report.plex_delivery.stream.language : ""}${report.plex_delivery.reason ? " · " + report.plex_delivery.reason : ""}`, "small"));
    if (report.runtime) $("detail-audit").append(element("p", `Inference: ${report.runtime.backend} · ${report.runtime.compute_type || ""}${report.runtime.fallback_reason ? " · " + report.runtime.fallback_reason : ""}`, "small"));
    const stats = [["Output cues", report.output_cues], ["Authored cues matched", `${report.preserved_cues} / ${report.source_cues}`],
      ["Recognized words outside authored cues", `${Math.round(report.generated_word_ratio * 100)}%`], ["Whisper model", report.model]];
    if (report.audit) {
      const {before, after, improved} = report.audit;
      const seconds = value => value == null ? "Insufficient evidence" : `${value.toFixed(2)} s`;
      stats.push(["Audit result", before.decision === "pass" ? "Passed — original retained" : before.decision === "repair" ? "Timing problem detected" : "Inconclusive"],
        ["Confident cue coverage", `${before.supported_cues} / ${before.total_cues}`],
        ["Original p95 boundary difference", seconds(before.p95_error_seconds)]);
      if (after) stats.push(["Candidate p95 boundary difference", seconds(after.p95_error_seconds)],
        ["Improvement check", improved ? "Passed" : "Failed"]);
      $("detail-audit").append(element("p", before.reason, "small"));
      const evidence = element("details", undefined, "small mb-3");
      evidence.append(element("summary", "Largest original timing differences (up to 10 cues)"));
      const list = element("ul", undefined, "mt-2");
      for (const item of [...before.evidence].sort((a,b) => b.error_seconds-a.error_seconds).slice(0,10)) {
        list.append(element("li", `Cue ${item.cue}: start ${item.start_delta_seconds.toFixed(2)} s, end ${item.end_delta_seconds.toFixed(2)} s relative to recognized speech.`));
      }
      evidence.append(list);
      const download = element("button", "Download full audit report", "btn btn-sm btn-outline-secondary mb-3");
      download.type = "button";
      download.addEventListener("click", () => {
        const url = URL.createObjectURL(new Blob([JSON.stringify(report, null, 2)], {type:"application/json"}));
        const link = element("a"); link.href = url; link.download = `crowbarr-audit-${job.id}.json`;
        link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
      });
      $("detail-audit").append(evidence, download);
    }
    for (const [key, value] of stats) $("detail-stats").append(element("dt", key, "col-sm-4"), element("dd", String(value), "col-sm-8"));
    for (const issue of report.issues || []) $("detail-issues").append(element("li", issue));
    for (const warning of report.warnings || []) $("detail-issues").append(element("li", warning, "text-secondary"));
    $("detail-issues").append(element("li", report.note, "text-secondary"));
  }
}
async function refresh() {
  if (!authenticated || polling) return;
  const revision = sessionRevision;
  polling = true;
  try {
    const status = await api("/status");
    if (!authenticated || revision !== sessionRevision) return;
    snapshot = status;
    $("version").textContent = snapshot.version;
    const resource = snapshot.resources || {};
    $("resource-state").textContent = [resource.gpu || "CPU runtime", resource.ram_available_mb != null ? `${resource.ram_available_mb} MB RAM headroom${resource.zfs_arc_reclaimable_mb ? " (including " + resource.zfs_arc_reclaimable_mb + " MB reclaimable ZFS ARC)" : ""}` : "", resource.vram_free_mb != null ? `${resource.vram_free_mb} / ${resource.vram_total_mb} MB GPU memory free` : "", snapshot.wait_reason || ""].filter(Boolean).join(" · ");
    $("service-state").textContent = snapshot.paused ? "Queue paused" : snapshot.configured ? (snapshot.discovery_mode === "arr" ? "Following arr libraries" : "Watching folders") : "Setup needed";
    $("activity-subtitle").textContent = snapshot.last_scan ?
      `${snapshot.media_count} media files found · Last checked ${relativeTime(snapshot.last_scan)}` : "Your library will be checked automatically.";
    $("setup-callout").hidden = snapshot.configured;
    $("pause").textContent = snapshot.paused ? "Resume queue" : "Pause queue";
    $("notices").replaceChildren();
    for (const notice of snapshot.notices) $("notices").append(element("div", notice.message, "alert alert-warning small"));
    if (!snapshot.ffmpeg) $("notices").append(element("div", "FFmpeg or ffprobe is missing. Install both before processing media.", "alert alert-warning small"));
    $("integration-status").replaceChildren();
    for (const name of snapshot.providers || []) {
      const sync = (snapshot.integrations || []).find(item => item.provider === name);
      const label = name === "sonarr" ? "Sonarr" : "Radarr";
      $("integration-status").append(element("span", !sync ? `${label}: first sync pending` :
        sync.healthy ? `${label}: ${sync.file_count} eligible files · synced ${relativeTime(sync.last_success)}` :
        `${label}: sync unavailable · new jobs held`, sync?.healthy ? "text-secondary" : "text-warning-emphasis"));
    }
    renderJobs();
  } catch (error) {
    if (authenticated && revision === sessionRevision) {
      $("activity-subtitle").textContent = "Queue update failed. Retrying automatically…";
      message(error.message, true);
    }
  }
  finally { polling = false; }
}
function fillSettings(settings) {
  configuration = settings;
  $("machine-api-key").value = settings.api_key || "";
  for (const field of $("settings-form").elements) {
    if (!field.name || !(field.name in settings)) continue;
    if (field.type === "checkbox") field.checked = settings[field.name];
    else field.value = field.name === "roots" ? settings.roots.join("\n") : settings[field.name];
  }
  $("connections").replaceChildren();
  for (const name of ["sonarr", "radarr", "bazarr", "plex"]) {
    const group = element("div", undefined, "connection-row");
    group.append(element("h3", name[0].toUpperCase() + name.slice(1), "h6 mb-3"));
    const fields = element("div", undefined, "row g-3");
    for (const [key, label, type] of [["url", "Service URL", "url"], ["api_key", name === "plex" ? "Plex token" : "API key", "password"]]) {
      const column = element("div", undefined, "col-sm-6");
      const inputLabel = element("label", label, "form-label");
      const input = element("input", undefined, "form-control");
      input.id = `${name}-${key}`; input.name = input.id; input.type = type; input.autocomplete = "off";
      inputLabel.htmlFor = input.id;
      input.value = key === "url" ? settings[name].url : "";
      if (key === "api_key" && settings[name].has_api_key) input.placeholder = "Saved · leave blank to keep";
      column.append(inputLabel, input); fields.append(column);
    }
    const test = element("button", "Test saved connection", "btn btn-sm btn-outline-secondary mt-3");
    test.type = "button";
    test.addEventListener("click", () => action(test, () => api(`/connections/${name}/test`, "POST")));
    group.append(fields);
    if (["sonarr", "radarr"].includes(name)) {
      const mappingLabel = element("label", "Path mappings", "form-label mt-3");
      const mappings = element("textarea", undefined, "form-control");
      mappings.id = `${name}-mappings`; mappings.rows = 2;
      mappings.placeholder = name === "sonarr" ? "/tv => /media/tv" : "/movies => /media/movies";
      mappings.value = (settings[name].mappings || []).map(m => `${m.remote} => ${m.local}`).join("\n");
      mappingLabel.htmlFor = mappings.id;
      const help = element("div", "One mapping per line: path in the media manager => path inside Crowbarr. Leave blank when paths are identical.", "form-text");
      help.id = `${name}-mapping-help`; mappings.setAttribute("aria-describedby", help.id);
      const check = element("div", undefined, "form-check mt-3");
      const input = element("input", undefined, "form-check-input");
      input.type = "checkbox"; input.id = `${name}-monitored`; input.checked = settings[name].monitored_only !== false;
      const label = element("label", name === "sonarr" ? "Only monitored series and episodes" : "Only monitored movies", "form-check-label");
      label.htmlFor = input.id; check.append(input, label);
      group.append(mappingLabel, mappings, help, check);
    }
    group.append(test); $("connections").append(group);
  }
}
async function action(button, callback) {
  button.disabled = true;
  try { const result = await callback(); if (result?.message) message(result.message); await refresh(); }
  catch (error) { message(error.message, true); }
  finally { button.disabled = false; }
}
async function enter() {
  const settings = await api("/settings");
  fillSettings(settings);
  authenticated = true;
  sessionRevision += 1;
  $("password").value = "";
  $("login-view").hidden = true;
  $("navigation").hidden = false;
  $("logout").hidden = false;
  message(""); view(activeView); await refresh();
}
let accountConfigured = true;
async function showLogin() {
  try {
    accountConfigured = (await (await fetch("/api/session")).json()).configured;
  } catch { accountConfigured = true; }
  $("login-heading").textContent = accountConfigured ? "Welcome to Crowbarr" : "Create your Crowbarr login";
  $("login-intro").textContent = accountConfigured
    ? "Automatic subtitles for your media library. Sign in to connect your folders and services."
    : "Choose a username and password for this dashboard. Nothing is sent anywhere; it is stored on your server.";
  $("key-help").textContent = accountConfigured
    ? "This is only for this dashboard. Sonarr, Radarr and Bazarr use the API key in Settings instead."
    : "At least 8 characters. You can change it later by deleting /config/dashboard.json.";
  $("login-submit").textContent = accountConfigured ? "Sign in" : "Create login";
  $("username").autocomplete = accountConfigured ? "username" : "off";
  $("password").autocomplete = accountConfigured ? "current-password" : "new-password";
}
$("login-form").addEventListener("submit", async event => {
  event.preventDefault();
  const credentials = { username: $("username").value, password: $("password").value };
  await action(event.submitter, async () => {
    const response = await fetch(accountConfigured ? "/api/session" : "/api/setup", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(credentials),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(typeof result.detail === "string" ? result.detail : "Sign in failed.");
    $("password").value = "";
    await enter();
    return result;
  });
});
$("settings-form").addEventListener("submit", async event => {
  event.preventDefault();
  const payload = structuredClone(configuration);
  for (const field of $("settings-form").elements) {
    if (!field.name || !(field.name in payload)) continue;
    payload[field.name] = field.type === "checkbox" ? field.checked : field.type === "number" ? Number(field.value) : field.value;
  }
  payload.roots = $("roots").value.split("\n").map(value => value.trim()).filter(Boolean);
  for (const name of ["sonarr", "radarr", "bazarr", "plex"]) {
    payload[name] = { url: $(`${name}-url`).value.trim(), api_key: $(`${name}-api_key`).value.trim() };
    if (["sonarr", "radarr"].includes(name)) {
      const lines = $(`${name}-mappings`).value.split("\n").map(line => line.trim()).filter(Boolean);
      if (lines.some(line => line.split("=>").length !== 2 || line.split("=>").some(part => !part.trim()))) {
        message(`Check ${name} mappings: each line must contain remote path => local path.`, true);
        $(`${name}-mappings`).focus(); return;
      }
      payload[name].mappings = lines.map(line => {const [remote, local] = line.split("=>").map(part => part.trim()); return {remote, local};});
      payload[name].monitored_only = $(`${name}-monitored`).checked;
    }
  }
  await action(event.submitter, async () => {
    fillSettings(await api("/settings", "PUT", payload));
    $("save-state").textContent = "Saved. Library check requested.";
    return { message: "Settings saved." };
  });
});
document.querySelectorAll("[data-view]").forEach(button => button.addEventListener("click", () => view(button.dataset.view)));
$("logout").addEventListener("click", signOut);
$("toggle-api-key").addEventListener("click", event => {
  const reveal = $("machine-api-key").type === "password";
  $("machine-api-key").type = reveal ? "text" : "password";
  event.currentTarget.textContent = reveal ? "Hide key" : "Show key";
  event.currentTarget.setAttribute("aria-pressed", String(reveal));
});
$("pause").addEventListener("click", event => action(event.currentTarget, () => api("/pause", "POST")));
$("scan").addEventListener("click", event => action(event.currentTarget, () => api("/scan", "POST")));
let searchTimer = null;
async function renderMediaResults(term) {
  const list = $("media-results");
  list.replaceChildren();
  if (term.trim().length < 2) return;
  let results;
  try {
    results = (await api(`/media?q=${encodeURIComponent(term.trim())}`)).results;
  } catch (error) {
    list.append(element("li", error.message, "list-group-item text-danger small"));
    return;
  }
  if (!results.length) {
    list.append(element("li", "No managed media matches that name.", "list-group-item text-secondary small"));
    return;
  }
  for (const item of results) {
    const row = element("li", undefined, "list-group-item d-flex flex-wrap justify-content-between align-items-center gap-2");
    row.append(element("span", item.title, "text-break"));
    const buttons = element("span", undefined, "d-flex gap-2");
    const request = (label, className, directive) => {
      const button = element("button", label, `btn btn-sm ${className}`);
      button.addEventListener("click", () => action(button, async () => {
        const result = await api("/process", "POST", { media: item.path, directive });
        await refresh();
        return result;
      }));
      return button;
    };
    buttons.append(request("Audit", "btn-outline-primary", ""));
    buttons.append(request("Generate fresh", "btn-outline-secondary", "generate"));
    row.append(buttons);
    list.append(row);
  }
}
$("media-search").addEventListener("input", event => {
  const term = event.target.value;
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => renderMediaResults(term), 250);
});
$("filter").addEventListener("change", renderJobs);
$("close-detail").addEventListener("click", closeDetails);
// The session lives in an httpOnly cookie, so resume straight into the dashboard
// when one is still valid and fall back to the sign-in panel when it is not.
enter().catch(() => showLogin());
setInterval(refresh, 5000);
