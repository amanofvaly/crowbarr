"use strict";
const $ = (id) => document.getElementById(id);
const esc = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (char) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        char
      ],
  );
const paths = {
  dashboard:
    '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>',
  activity: '<path d="M3 12h4l3-8 4 16 3-8h4"/>',
  library:
    '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 4v16M17 4v16M3 9h4m-4 6h4m10-6h4m-4 6h4"/>',
  review: '<path d="M12 3 2 20h20L12 3Z"/><path d="M12 9v5m0 3h.01"/>',
  history: '<path d="M3 11a9 9 0 1 1 2 7M3 4v7h7m2-4v6l4 2"/>',
  settings:
    '<path d="M4 7h16M4 17h16"/><circle cx="9" cy="7" r="3"/><circle cx="15" cy="17" r="3"/>',
  api: '<path d="m8 6-6 6 6 6m8-12 6 6-6 6m-3-15-2 18"/>',
  search: '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/>',
  check: '<path d="m5 12 4 4L19 6"/>',
  refresh:
    '<path d="M20 7a9 9 0 0 0-16 2m0-5v5h5M4 17a9 9 0 0 0 16-2m0 5v-5h-5"/>',
  pause: '<path d="M8 5v14M16 5v14"/>',
  play: '<path d="m8 4 13 8-13 8V4Z"/>',
  arrow: '<path d="M4 12h16m-6-6 6 6-6 6"/>',
  close: '<path d="m6 6 12 12M6 18 18 6"/>',
  menu: '<path d="M4 6h16M4 12h16M4 18h16"/>',
  cpu: '<rect x="6" y="6" width="12" height="12" rx="2"/><path d="M9 2v4m6-4v4M9 18v4m6-4v4M2 9h4m-4 6h4m12-6h4m-4 6h4"/>',
  queue: '<path d="M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01"/>',
  copy: '<rect x="8" y="8" width="12" height="13" rx="2"/><path d="M16 8V3H3v13h5"/>',
};
const icon = (name) =>
  `<svg class="icon" viewBox="0 0 24 24" aria-hidden="true">${paths[name] || paths.activity}</svg>`;
const button = (label, action, style = "", attrs = "") =>
  `<button type="button" class="btn button ${style.includes("primary") ? "btn-primary" : "btn-outline-secondary"} ${style}" data-action="${action}" ${attrs}>${label}</button>`;
const link = (label, route, style = "") =>
  `<a class="btn button ${style.includes("primary") ? "btn-primary" : "btn-outline-secondary"} ${style}" href="#${route}">${label}</a>`;
const labels = {
  processing: "Processing",
  queued: "Queued",
  waiting: "Waiting for subtitle",
  retry: "Retry scheduled",
  completed: "Subtitle written",
  unchanged: "Original retained",
  review: "Review needed",
  failed: "Failed",
  superseded: "Superseded",
  cancelled: "Cancelled",
};
const origins = {
  manual: "Manual request",
  import: "New import",
  bazarr: "Bazarr subtitle",
  retry: "Retry",
  backlog: "Library sweep",
};
const badge = (state) =>
  `<span class="badge ${esc(state)}">${state === "processing" ? '<span class="status-dot"></span>' : ""}${esc(labels[state] || state)}</span>`;
const fmt = (value) => Number(value || 0).toLocaleString();
const duration = (value) =>
  value == null
    ? "Unavailable"
    : `${Math.floor(Math.max(0, value) / 60)}m ${Math.floor(Math.max(0, value) % 60)}s`;
const ago = (value) => {
  if (!value) return "Not yet";
  const seconds = Math.max(0, Date.now() / 1000 - value);
  if (seconds < 60) return "Just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)} minutes ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} hours ago`;
  return `${Math.floor(seconds / 86400)} days ago`;
};
const title = (job) =>
  job.title || job.media?.split("/").pop() || "Untitled media";
let savedSettings,
  settings,
  status,
  session = false,
  busy = false,
  route = "dashboard",
  section = "connections",
  pageOffset = 0;
let listQuery = "",
  libraryProvider = "all",
  historyFilter = "history",
  routeRevision = 0,
  listRevision = 0,
  dirty = false,
  setup = false,
  timer;
let jobs = new Map(),
  media = [],
  total = 0,
  toastTimer;
const navigation = [
  ["dashboard", "Dashboard"],
  ["activity", "Activity"],
  ["library", "Library"],
  ["review", "Review"],
  ["history", "History"],
  ["settings", "Settings"],
  ["api", "API & Webhooks"],
];
function toast(text, error = false) {
  $("toast").textContent = text;
  $("toast").className = `toast show${error ? " error" : ""}`;
  $("toast").hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => ($("toast").hidden = true), 6500);
}
async function api(path, method = "GET", body) {
  const response = await fetch(`/api${path}`, {
    method,
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const result = await response.json();
  if (!response.ok) {
    if (response.status === 401 && session) {
      session = false;
      showLogin();
    }
    throw new Error(
      typeof result.detail === "string"
        ? result.detail
        : "Request failed. Check your connection and try again.",
    );
  }
  return result;
}
function empty(heading, description, action = "", symbol = "queue") {
  return `<div class="empty">${icon(symbol)}<h3>${heading}</h3><p>${description}</p>${action}</div>`;
}
function heading(name, description, actions = "") {
  return `<div class="page-heading"><div><h1>${name}</h1><p>${description}</p></div><div class="actions">${actions}</div></div>`;
}
function panel(name, body, extra = "") {
  return `<section class="panel"><div class="panel-header"><h2>${name}</h2>${extra}</div>${body}</section>`;
}
function drawNav() {
  const counts = status?.counts || {};
  $("navigation").innerHTML = navigation
    .map(
      ([key, label], index) =>
        `${index === 5 ? '<div class="nav-separator"></div>' : ""}<a href="#${key}" class="nav-link ${key === route ? "active" : ""}" ${key === route ? 'aria-current="page"' : ""}>${icon(key)}${label}${key === "activity" ? `<span class="count">${fmt(["processing", "queued", "waiting", "retry"].reduce((sum, k) => sum + (counts[k] || 0), 0))}</span>` : key === "review" ? `<span class="count">${fmt((counts.review || 0) + (counts.failed || 0))}</span>` : ""}</a>`,
    )
    .join("");
  $("breadcrumb").textContent =
    `Workspace / ${navigation.find(([key]) => key === route)?.[1] || "Dashboard"}`;
}
function progressMarkup(job, compact = false) {
  const measured =
    Number.isFinite(job.progress_current) && job.progress_total > 0;
  const percent = measured
    ? Math.min(
        100,
        Math.max(0, (job.progress_current / job.progress_total) * 100),
      )
    : null;
  return measured
    ? `<div class="${compact ? "inline-progress" : ""}"><div class="progress-label"><span>${compact ? "Audio processed" : esc(job.stage || "Recognizing dialogue")}</span><strong>${percent.toFixed(1)}%</strong></div><progress aria-label="Audio processed during recognition" max="${job.progress_total}" value="${job.progress_current}"></progress>${compact ? "" : `<p class="hint mt-8">${duration(job.progress_current)} of ${duration(job.progress_total)} audio · recognition stage</p>`}</div>`
    : `<div class="stage-working">${icon("refresh")}<span>${esc(job.stage || "Preparing worker")}</span></div>`;
}
function jobButtons(job) {
  const id = `data-id="${job.id}"`;
  return `${button("Details", "details", "small", id)}${["queued", "waiting", "retry"].includes(job.state) ? button("Run next", "promote", "small", id) : ""}${["queued", "waiting", "retry", "processing"].includes(job.state) ? button(job.cancel_requested ? "Cancelling…" : "Cancel", "cancel", "small danger", `${id} ${job.cancel_requested ? "disabled" : ""}`) : ["failed", "review", "unchanged", "completed"].includes(job.state) ? button("Retry", "retry", "small", id) : ""}`;
}
function activeJob() {
  const job = status?.jobs.find((j) => j.state === "processing");
  if (!job)
    return `<div class="worker-band idle">${icon(status?.paused ? "pause" : "check")}<strong>${status?.paused ? "Queue paused" : status?.wait_reason ? "Worker waiting" : "Worker idle"}</strong><span>${esc(status?.wait_reason || "No job is currently processing.")}</span></div>`;
  return `<div class="worker-band"><div class="worker-media">${badge("processing")}<div class="active-title">${esc(title(job))}</div><span class="hint">${esc(origins[job.origin] || "Library sweep")} · ${job.started ? duration(Date.now() / 1000 - job.started) + " elapsed" : "Starting"}</span></div><div class="worker-progress">${progressMarkup(job)}</div><div class="actions">${jobButtons(job)}</div></div>`;
}

function rows(items, compact = false) {
  items.forEach((j) => jobs.set(j.id, j));
  return `<div class="table-wrap"><table><thead><tr><th>Media</th><th>Status</th>${compact ? "" : "<th>Requested</th>"}<th>Actions</th></tr></thead><tbody>${items.map((j) => `<tr><td class="title-cell"><button class="title-link" data-action="details" data-id="${j.id}">${esc(title(j))}</button><small>${esc(origins[j.origin] || "Library sweep")}${j.error ? ` · ${esc(j.error)}` : j.state === "waiting" ? ` · Eligible ${esc(new Date(j.ready * 1000).toLocaleString())}` : ""}</small></td><td>${badge(j.state)}${j.state === "processing" ? progressMarkup(j, true) : ""}</td>${compact ? "" : `<td class="muted">${esc(ago(j.created))}</td>`}<td><div class="row-actions">${compact ? button("Details", "details", "small", `data-id="${j.id}"`) : jobButtons(j)}</div></td></tr>`).join("")}</tbody></table></div>`;
}
function systemPanel() {
  const r = status?.resources || {};
  return `<div class="panel-body">${[
    ["Detected GPU", r.gpu || "Not detected"],
    ["Whisper model", savedSettings?.model || "Unavailable"],
    [
      "RAM headroom",
      r.ram_available_mb == null
        ? "Unavailable"
        : `${fmt(r.ram_available_mb)} MB`,
    ],
    [
      "GPU memory free",
      r.vram_free_mb == null
        ? "Unavailable"
        : `${fmt(r.vram_free_mb)} / ${fmt(r.vram_total_mb)} MB`,
    ],
    [
      "CPU load / core",
      r.cpu_load == null ? "Unavailable" : Number(r.cpu_load).toFixed(2),
    ],
    ["CPU fallback", savedSettings?.cpu_fallback ? "Enabled" : "Disabled"],
  ]
    .map(
      ([key, value]) =>
        `<div class="system-row"><span>${key}</span><strong>${esc(value)}</strong></div>`,
    )
    .join(
      "",
    )}${status?.wait_reason ? `<p class="hint mt-18">${esc(status.wait_reason)}</p>` : ""}</div>`;
}
function connectionsPanel() {
  return `<div class="panel-body">${["sonarr", "radarr", "bazarr", "plex"]
    .map((name) => {
      const sync = status?.integrations?.find((i) => i.provider === name),
        connected = Boolean(savedSettings?.[name]?.url);
      return `<div class="service-row"><span class="service-mark">${name[0].toUpperCase()}</span><div><strong>${name[0].toUpperCase() + name.slice(1)}</strong><p>${!connected ? "Not configured" : sync ? (sync.healthy ? `${fmt(sync.file_count)} files · synced ${ago(sync.last_success)}` : "Sync unavailable") : "Configured · test in Settings"}</p></div><span class="status-dot ${!sync?.healthy ? "warning" : ""}"></span></div>`;
    })
    .join("")}</div>`;
}
function dashboard() {
  const counts = status?.counts || {};
  const pending = (status?.jobs || [])
    .filter((j) => ["queued", "waiting", "retry"].includes(j.state))
    .slice(0, 10);
  const recent = (status?.jobs || [])
    .filter(
      (j) => !["processing", "queued", "waiting", "retry"].includes(j.state),
    )
    .slice(0, 5);
  const waiting = ["queued", "waiting", "retry"].reduce(
    (sum, key) => sum + (counts[key] || 0),
    0,
  );
  const r = status?.resources || {};
  return (
    heading(
      "Dashboard",
      "",
      button(
        icon(status?.paused ? "play" : "pause") +
          (status?.paused ? "Resume queue" : "Pause queue"),
        "pause",
      ) +
        button(icon("refresh") + "Sync libraries", "scan") +
        link(icon("search") + "Search library", "library"),
    ) +
    (!status?.configured
      ? `<div class="callout"><div><strong>No libraries configured</strong><p>Connect a media manager or add your media folders to begin.</p></div>${link("Configure libraries", "settings/connections", "primary")}</div>`
      : "") +
    `<div class="overview-line"><span><strong>${fmt(status?.media_count)}</strong> media files</span><span><strong>${fmt(waiting)}</strong> pending</span><span><strong>${fmt(counts.completed)}</strong> subtitles written</span><a href="#review"><strong>${fmt((counts.review || 0) + (counts.failed || 0))}</strong> need review</a><span class="last-sync">Library sync: ${esc(ago(status?.last_scan))}</span></div>` +
    activeJob() +
    panel(
      "Queue",
      pending.length
        ? rows(pending)
        : empty(
            "Queue is empty",
            "Imported media will be queued automatically.",
          ),
      `<a href="#activity">View all ${fmt(waiting)} pending jobs</a>`,
    ) +
    `<div class="system-line"><span>${icon("cpu")} ${esc(r.gpu || "No GPU detected")}</span><span>RAM available: ${r.ram_available_mb == null ? "Unavailable" : fmt(r.ram_available_mb) + " MB"}</span><span>GPU memory free: ${r.vram_free_mb == null ? "Unavailable" : fmt(r.vram_free_mb) + " MB"}</span><span>Model: ${esc(savedSettings?.model || "Unavailable")}</span><a href="#settings/resources">Resource settings</a></div>` +
    panel(
      "Recent activity",
      recent.length
        ? rows(recent, true)
        : empty("No activity yet", "Finished jobs will appear here."),
      '<a href="#history">View history</a>',
    ) +
    `<details class="service-disclosure"><summary>Services & system information</summary><div class="system-details">${systemPanel()}${connectionsPanel()}</div></details>`
  );
}

function pager() {
  return `<div class="pagination"><span>${total ? `${fmt(pageOffset + 1)}–${fmt(Math.min(pageOffset + 25, total))} of ${fmt(total)}` : "0 results"}</span><div class="actions">${button("Previous", "previous", "small", pageOffset === 0 ? "disabled" : "")}${button("Next", "next", "small", pageOffset + 25 >= total ? "disabled" : "")}</div></div>`;
}
function listShell() {
  const names = {
    activity: [
      "Activity queue",
      "Prioritize requests, follow real progress, and manage pending work.",
    ],
    review: [
      "Needs review",
      "Inspect uncertain subtitles and failed jobs before taking action.",
    ],
    history: [
      "History",
      "Every audit, repair, and generation — with its outcome and evidence.",
    ],
  };
  const [name, description] = names[route];
  return (
    heading(
      name,
      description,
      route === "activity"
        ? button(status?.paused ? "Resume queue" : "Pause queue", "pause") +
            button("Sync libraries", "scan", "primary")
        : link("Search library", "library"),
    ) +
    `<section class="panel"><div class="queue-toolbar"><label class="sr-only" for="queue-query">Search jobs</label><input type="search" id="queue-query" placeholder="Filter by title or file path…" value="${esc(listQuery)}">${route === "history" ? `<label class="sr-only" for="history-filter">Filter outcomes</label><select id="history-filter">${["history", "completed", "unchanged", "review", "failed", "cancelled", "superseded"].map((s) => `<option value="${s}" ${historyFilter === s ? "selected" : ""}>${s === "history" ? "All outcomes" : labels[s]}</option>`).join("")}</select>` : ""}<span class="hint">${route === "activity" ? "Manual requests run ahead of background work" : "Results from your server"}</span></div><div id="list-content" aria-live="polite"><div class="loading">Loading ${name.toLowerCase()}…</div></div></section>`
  );
}
function libraryShell() {
  return (
    heading(
      "Library",
      "Find a movie or episode. Check its subtitles or request a fresh transcript.",
      button(icon("refresh") + "Sync libraries", "scan"),
    ) +
    `<div class="search-hero">${icon("search")}<label class="sr-only" for="library-query">Search library</label><input id="library-query" type="search" placeholder="Search titles, episodes, or file paths…" value="${esc(listQuery)}" autocomplete="off"><label class="sr-only" for="library-provider">Library source</label><select id="library-provider">${[
      ["all", "All sources"],
      ["sonarr", "Sonarr · TV"],
      ["radarr", "Radarr · Movies"],
      ["folders", "Folders"],
    ]
      .map(
        ([value, label]) =>
          `<option value="${value}" ${libraryProvider === value ? "selected" : ""}>${label}</option>`,
      )
      .join(
        "",
      )}</select></div><section class="panel"><div class="panel-header"><h2>Media library</h2><span class="hint">Audit preserves usable authored subtitles</span></div><div id="list-content" aria-live="polite"><div class="loading">Loading your library…</div></div></section>`
  );
}
async function loadList(silent = false) {
  const revision = ++listRevision,
    view = route;
  if (!["library", "activity", "review", "history"].includes(view)) return;
  try {
    const params = new URLSearchParams({
      q: listQuery,
      offset: pageOffset,
      limit: 25,
      ...(view === "library"
        ? { provider: libraryProvider }
        : {
            state:
              view === "activity"
                ? "queue"
                : view === "history"
                  ? historyFilter
                  : "review",
          }),
    });
    const result = await api(
      `/${view === "library" ? "media" : "jobs"}?${params}`,
    );
    if (revision !== listRevision || view !== route || !session) return;
    total = result.total;
    if (pageOffset >= total && pageOffset > 0) {
      pageOffset = Math.max(0, Math.floor((total - 1) / 25) * 25);
      return loadList();
    }
    if (view === "library") {
      media = result.results;
      $("list-content").innerHTML = media.length
        ? `<div class="table-wrap"><table><thead><tr><th>Title / file</th><th>Source</th><th>Subtitle actions</th></tr></thead><tbody>${media.map((item, index) => `<tr><td class="title-cell"><strong>${esc(item.label !== item.path ? item.label : item.title)}</strong><small>${esc(item.path)}</small></td><td><span class="badge">${esc(item.provider === "folders" ? "Folders" : item.provider === "sonarr" ? "Sonarr" : "Radarr")}</span></td><td><div class="row-actions">${button("Audit subtitles", "audit", "small primary", `data-index="${index}"`)}${button("Generate fresh", "generate", "small", `data-index="${index}"`)}</div></td></tr>`).join("")}</tbody></table></div>${pager()}`
        : empty(
            listQuery ? "No matching media" : "Your library is waiting",
            listQuery
              ? "Try a shorter title, episode number, or another source."
              : "Connect your media managers or folders, then sync your library.",
            link("Manage libraries", "settings/connections"),
            "search",
          );
    } else {
      $("list-content").innerHTML = result.results.length
        ? rows(result.results) + pager()
        : empty(
            listQuery
              ? "No matching jobs"
              : view === "review"
                ? "Nothing needs your attention"
                : view === "activity"
                  ? "The queue is clear"
                  : "No history yet",
            listQuery
              ? "Try another title or clear the filter."
              : view === "review"
                ? "Uncertain candidates and failed jobs appear here for inspection."
                : "Results will appear as Crowbarr processes your library.",
            link("Browse library", "library"),
            view === "review" ? "check" : "queue",
          );
    }
  } catch (error) {
    if (revision === listRevision && $("list-content") && !silent)
      $("list-content").innerHTML = empty(
        "Couldn’t load this view",
        esc(error.message),
        button("Try again", "reload"),
        "review",
      );
  }
}
const groups = [
  ["connections", "Connections"],
  ["library", "Media & discovery"],
  ["processing", "Speech processing"],
  ["resources", "Resources & schedule"],
  ["quality", "Quality & retries"],
  ["appearance", "Appearance"],
];
const field = (name, label, help = "", type = "text", extra = "") =>
  `<label>${label}<input name="${name}" type="${type}" value="${esc(settings[name])}" ${extra}><small>${help}</small></label>`;
const numeric = (name, label, min, max, help = "", step = 1) =>
  field(
    name,
    label,
    help,
    "number",
    `min="${min}" max="${max}" step="${step}" required`,
  );
const check = (name, label, help = "") =>
  `<label class="check-field wide"><input name="${name}" type="checkbox" ${settings[name] ? "checked" : ""}><span>${label}<small>${help}</small></span></label>`;
const select = (name, label, options, help = "") =>
  `<label>${label}<select name="${name}">${options
    .map((value) => {
      const [id, text] = Array.isArray(value) ? value : [value, value];
      return `<option value="${id}" ${settings[name] === id ? "selected" : ""}>${text}</option>`;
    })
    .join("")}</select><small>${help}</small></label>`;
function settingPanel(name, description, fields) {
  return `<section class="panel"><div class="panel-header"><div><h2>${name}</h2><p>${description}</p></div></div><div class="field-grid">${fields}</div></section>`;
}
function connectionSettings() {
  return ["sonarr", "radarr", "bazarr", "plex"]
    .map((name) => {
      const arr = ["sonarr", "radarr"].includes(name),
        c = settings[name];
      return settingPanel(
        name[0].toUpperCase() + name.slice(1),
        arr
          ? "Library membership, imported media, and monitoring preferences."
          : name === "bazarr"
            ? "Authored subtitle discovery and alternative providers."
            : "Playback awareness and subtitle discovery.",
        `<label>Service URL<input type="url" name="${name}.url" value="${esc(c.url)}" placeholder="http://${name}:${{ sonarr: 8989, radarr: 7878, bazarr: 6767, plex: 32400 }[name]}" autocomplete="off"><small>Address reachable from the Crowbarr server.</small></label><label>${name === "plex" ? "Plex token" : "API key"}<input type="password" name="${name}.api_key" value="${esc(c.api_key || "")}" placeholder="${c.has_api_key ? "Saved · leave blank to keep" : "Enter service key"}" autocomplete="new-password"><small>Save changes before testing this connection.</small></label>${arr || name === "plex" ? `<label class="wide">Path mappings<textarea name="${name}.mappings" rows="2" placeholder="/remote/path => /local/path">${esc((arr ? c.mappings : settings.plex_mappings).map((m) => `${m.remote} => ${m.local}`).join("\n"))}</textarea><small>${name === "plex" ? "Crowbarr path => Plex path." : "Media manager path => Crowbarr path."} One mapping per line; leave blank when paths match.</small></label>` : ""}${arr ? `<label class="check-field wide"><input type="checkbox" name="${name}.monitored_only" ${c.monitored_only ? "checked" : ""}><span>Only monitored ${name === "sonarr" ? "series and episodes" : "movies"}</span></label>` : ""}<div class="wide actions">${button("Test saved connection", "test", "", `data-name="${name}" ${!c.url ? "disabled" : ""}`)}<span class="hint">${c.url ? "Clear the URL and save to disconnect." : "Not configured"}</span></div>`,
      );
    })
    .join("");
}
function settingsPage() {
  let content = "";
  if (section === "connections") content = connectionSettings();
  if (section === "library")
    content = settingPanel(
      "Media & discovery",
      "Define readable media folders and how often the library is reconciled.",
      `<label class="wide">Media folders<textarea name="roots" rows="4" placeholder="/media/tv&#10;/media/movies">${esc(settings.roots.join("\n"))}</textarea><small>One absolute path per line. Crowbarr needs permission to write subtitles beside videos. Mapped media-manager roots are also authorized.</small></label>${numeric("scan_seconds", "Sync interval (seconds)", 10, 86400)}${numeric("settle_seconds", "File settling time (seconds)", 0, 86400, "Wait for files to stop changing before processing.")}${numeric("subtitle_wait_minutes", "Wait for Bazarr (minutes)", 0, 10080, "After this window, check embedded subtitles, then generate.")}`,
    );
  if (section === "processing")
    content = settingPanel(
      "Speech processing",
      "English dialogue and same-language subtitles are supported.",
      select(
        "model",
        "Whisper model",
        ["tiny", "base", "small", "medium", "large-v3", "large-v3-turbo"],
        "Larger models need more memory.",
      ) +
        select("device", "Processing device", [
          ["cpu", "CPU"],
          ["cuda", "NVIDIA GPU · CUDA"],
        ]) +
        select(
          "compute_type",
          "Compute precision",
          ["int8", "float32", "int8_float16", "float16"],
          "CPU supports INT8 and FP32.",
        ) +
        numeric("cpu_threads", "CPU threads", 1, 64) +
        check(
          "cpu_fallback",
          "Fall back to CPU",
          "Use CPU when CUDA initialization fails.",
        ) +
        check(
          "refine_generated",
          "Refine generated timings with WhisperX",
          "Requires the optional alignment package and additional memory.",
        ) +
        check("allow_untagged_audio", "Try audio without a language tag") +
        check(
          "allow_untagged_subtitles",
          "Treat untagged SRT files as English",
        ),
    );
  if (section === "resources")
    content =
      settingPanel(
        "Resource limits",
        "A single worker processes inference jobs. Memory limits apply to every job.",
        numeric("min_free_ram_mb", "Minimum RAM headroom (MB)", 128, 262144) +
          numeric(
            "min_free_vram_mb",
            "Minimum free GPU memory (MB)",
            128,
            262144,
          ) +
          numeric(
            "max_cpu_load",
            "Maximum background CPU load per core",
            0.1,
            4,
            "Normalized load average; 1 means one runnable task per core.",
            0.1,
          ),
      ) +
      settingPanel(
        "Background schedule",
        "Manual and import jobs bypass cooldown, quiet hours, and hourly budget.",
        numeric(
          "backlog_cooldown_seconds",
          "Cooldown between jobs (seconds)",
          0,
          86400,
        ) +
          numeric(
            "background_budget_minutes",
            "Processing budget per hour (minutes)",
            1,
            60,
          ) +
          numeric(
            "quiet_hour_start",
            "Quiet hours start (server hour)",
            0,
            23,
            "Use equal start and end hours to disable quiet hours.",
          ) +
          numeric("quiet_hour_end", "Quiet hours end (server hour)", 0, 23) +
          check(
            "defer_during_plex",
            "Defer background processing during Plex playback",
          ),
      );
  if (section === "quality")
    content = settingPanel(
      "Audit & recovery",
      "Control quality thresholds and recovery for uncertain or failed work.",
      check(
        "sampled_audit",
        "Sample dialogue across the runtime",
        "Escalate inconclusive samples to full dialogue recognition.",
      ) +
        check(
          "bazarr_download_alternatives",
          "Download alternatives from Bazarr",
          "Allow alternative authored candidates during processing.",
        ) +
        numeric("max_provider_attempts", "Maximum provider attempts", 1, 10) +
        numeric("max_attempts", "Maximum job attempts", 1, 10) +
        numeric("job_timeout_minutes", "Job timeout (minutes)", 1, 1440) +
        numeric(
          "min_match_ratio",
          "Minimum authored match ratio",
          0.5,
          1,
          "Fraction of authored cues required to match.",
          0.01,
        ) +
        numeric(
          "min_alignment_score",
          "Minimum alignment score",
          0,
          1,
          "Lower confidence is sent for review.",
          0.01,
        ) +
        numeric(
          "max_generated_ratio",
          "Maximum generated word ratio",
          0,
          1,
          "Fraction of recognized words outside authored cues.",
          0.01,
        ),
    );
  if (section === "appearance")
    content = settingPanel(
      "Appearance",
      "Choose a theme for this browser. Your server settings are shared separately.",
      `<label>Color theme<select id="theme-select"><option value="dark" ${document.documentElement.dataset.theme === "dark" ? "selected" : ""}>Dark</option><option value="light" ${document.documentElement.dataset.theme === "light" ? "selected" : ""}>Light</option></select><small>Saved on this device.</small></label>`,
    );
  return (
    heading("Settings", "") +
    `<div class="settings-layout"><nav class="settings-nav" aria-label="Settings sections">${groups.map(([id, label]) => `<a href="#settings/${id}" class="nav-link ${id === section ? "active" : ""}" ${id === section ? 'aria-current="page"' : ""}>${label}</a>`).join("")}</nav><form id="settings-form" class="settings-form">${content}${section !== "appearance" ? `<div class="save-bar"><button class="button primary" type="submit">Save changes</button><span id="save-state" class="settings-status">All changes saved</span></div>` : ""}</form></div>`
  );
}
function apiPage() {
  const endpoints = [
    [
      "GET",
      "/api/status",
      "Worker progress, queue totals, resources, and integration health.",
    ],
    [
      "GET",
      "/api/jobs?state=queue&offset=0&limit=25",
      "Paginated jobs. Filter by state and title using q.",
    ],
    ["GET", "/api/jobs/{id}", "Full details and audit evidence for one job."],
    [
      "GET",
      "/api/media?q=title&provider=all",
      "Search managed media. Supports offset and limit.",
    ],
    [
      "POST",
      "/api/process",
      "Request an audit or fresh generation for a managed media path.",
    ],
    ["POST", "/api/jobs/{id}/promote", "Prioritize a pending job."],
    ["POST", "/api/jobs/{id}/cancel", "Request cancellation."],
    [
      "POST",
      "/api/jobs/{id}/retry",
      "Requeue an eligible completed or failed job.",
    ],
    ["GET", "/api/jobs/{id}/candidate", "Download a private review subtitle."],
    [
      "POST",
      "/api/jobs/{id}/approve",
      "Publish a reviewed subtitle candidate.",
    ],
    ["POST", "/api/scan", "Request library reconciliation."],
    ["POST", "/api/pause", "Toggle queue pause."],
    [
      "GET",
      "/api/settings",
      "Read configuration. Includes the administrative API key.",
    ],
    ["PUT", "/api/settings", "Save server configuration."],
  ];
  return (
    heading(
      "API & Webhooks",
      "Connect your apps to Crowbarr with an authenticated HTTP API.",
      '<a class="button" href="/api/openapi.json" download="crowbarr-openapi.json">Download OpenAPI schema</a>',
    ) +
    `<div class="api-grid"><div>${panel(
      "API access",
      `<div class="panel-body"><p class="muted">Use <code>X-Api-Key</code> or <code>Authorization: Bearer</code>. This key grants administrative access, including settings and publication.</p><div class="api-key"><label class="sr-only" for="api-key">Crowbarr API key</label><input id="api-key" readonly type="password" value="${esc(settings.api_key)}" autocomplete="off">${button("Show", "reveal")}${button(icon("copy") + "Copy", "copy-key")}</div><p class="hint mt-12">Keep this key in your app’s secret storage. Dashboard sessions use a separate login.</p><h3 class="mt-24">Base URL</h3><pre>${esc(location.origin)}/api</pre><h3>Request an audit</h3><pre>curl -X POST '${esc(location.origin)}/api/process' \
  -H 'X-Api-Key: YOUR_API_KEY' \
  -H 'Content-Type: application/json' \
  -d '{"media":"/media/movies/example.mkv"}'</pre><p class="hint">Use an actual path returned by the library endpoint. Add <code>"directive":"generate"</code> to request fresh generation. A 202 response includes the job ID; poll its details for progress.</p></div>`,
    )}${panel("Sonarr & Radarr webhooks", `<div class="panel-body"><p class="muted">Add a Webhook connection in each media manager for imports, upgrades, renames, and deletions.</p>${["sonarr", "radarr"].map((name) => `<h3 class="mt-20">${name[0].toUpperCase() + name.slice(1)}</h3><pre>${esc(location.origin)}/api/hooks/${name}</pre>`).join("")}<div class="system-row"><span>Basic auth username</span><strong>crowbarr</strong></div><div class="system-row"><span>Basic auth password</span><strong>Your Crowbarr API key</strong></div><p class="hint mt-16">Replace the host above with an address reachable from your media manager. Scheduled reconciliation covers missed events.</p></div>`)}</div><div>${panel("Endpoint reference", endpoints.map(([method, path, description]) => `<div class="endpoint"><span class="method ${method.toLowerCase()}">${method}</span><code>${esc(path)}</code><p>${description}</p></div>`).join(""))}</div></div>`
  );
}
async function navigate() {
  if (!session) return;
  const [next, sub] = location.hash.slice(1).split("/");
  const target = navigation.some(([id]) => id === next) ? next : "dashboard";
  if (dirty) {
    captureSettings();
    dirty = false;
  }
  route = target;
  section = groups.some(([id]) => id === sub) ? sub : "connections";
  routeRevision++;
  listRevision++;
  pageOffset = 0;
  listQuery = "";
  jobs = new Map();
  $("workspace").classList.remove("menu-open");
  $("menu").setAttribute("aria-expanded", "false");
  drawNav();
  $("page").innerHTML =
    route === "dashboard"
      ? dashboard()
      : route === "settings"
        ? settingsPage()
        : route === "api"
          ? apiPage()
          : route === "library"
            ? libraryShell()
            : listShell();
  if (route === "settings" && draftDirty) {
    dirty = true;
    $("save-state") && ($("save-state").textContent = "Unsaved changes");
  }
  document.title = `${navigation.find(([id]) => id === route)[1]} · Crowbarr`;
  await loadList();
}
let draftDirty = false;
function captureSettings() {
  const form = $("settings-form");
  if (!form) return;
  for (const el of form.elements) {
    if (!el.name) continue;
    const [name, key] = el.name.split(".");
    const value =
      el.type === "checkbox"
        ? el.checked
        : el.type === "number"
          ? Number(el.value)
          : el.value;
    if (key === "mappings") {
      const lines = value
        .split("\n")
        .map((s) => s.trim())
        .filter(Boolean);
      const mappings = lines.map((line) => {
        const parts = line.split("=>");
        if (parts.length !== 2 || parts.some((v) => !v.trim()))
          throw new Error(
            "Each path mapping needs a remote path => local path.",
          );
        return { remote: parts[0].trim(), local: parts[1].trim() };
      });
      if (name === "plex") settings.plex_mappings = mappings;
      else settings[name].mappings = mappings;
    } else if (key) settings[name][key] = value;
    else
      settings[name] =
        name === "roots"
          ? value
              .split("\n")
              .map((v) => v.trim())
              .filter(Boolean)
          : value;
  }
}
function renderDashboard() {
  const focused = document.activeElement?.closest("[data-action]");
  const focusAction = focused?.dataset.action,
    focusId = focused?.dataset.id;
  const previous = $("page").querySelector("progress")?.value;
  const oldTitle = $("page").querySelector(".active-title")?.textContent;
  $("page").innerHTML = dashboard();
  const progress = $("page").querySelector("progress"),
    next = progress?.value;
  if (
    progress &&
    previous != null &&
    oldTitle === $("page").querySelector(".active-title")?.textContent &&
    previous < next
  ) {
    progress.value = previous;
    requestAnimationFrame(() =>
      requestAnimationFrame(() => {
        if (progress.isConnected) progress.value = next;
      }),
    );
  }
  if (focusAction) {
    const button = [...$("page").querySelectorAll("[data-action]")].find(
      (b) => b.dataset.action === focusAction && b.dataset.id === focusId,
    );
    button?.focus({ preventScroll: true });
  }
}
async function refresh() {
  if (!session || busy) return;
  busy = true;
  try {
    const next = await api("/status");
    if (!session) return;
    status = next;
    status.jobs.forEach((j) => jobs.set(j.id, j));
    $("connection-status").textContent = "Live · updated just now";
    $("worker-state").textContent = status.paused
      ? "Queue paused"
      : status.wait_reason
        ? "Worker deferred"
        : status.jobs.some((j) => j.state === "processing")
          ? "Worker processing"
          : "Worker ready";
    $("worker-dot").classList.toggle(
      "warning",
      status.paused || Boolean(status.wait_reason),
    );
    $("version").textContent = `v${status.version} · Self-hosted`;
    drawNav();
    const notices = (status.notices || []).map((n) => n.message);
    if (!status.ffmpeg)
      notices.push(
        "FFmpeg is unavailable. Install FFmpeg and ffprobe to process media.",
      );
    $("notice").textContent = notices.join(" · ");
    $("notice").hidden = !notices.length;
    if (route === "dashboard" && !$("detail-dialog").open) renderDashboard();
    else if (
      ["activity", "review", "history"].includes(route) &&
      !$("detail-dialog").open &&
      !document.activeElement?.matches("input,select,button:disabled")
    )
      await loadList(true);
  } catch (error) {
    $("connection-status").textContent = "Connection interrupted";
    $("notice").textContent =
      `Live updates unavailable. Displaying the last received data. ${error.message} Retrying automatically.`;
    $("notice").hidden = false;
  } finally {
    busy = false;
  }
}
async function detail(id) {
  const job = await api(`/jobs/${id}`);
  jobs.set(job.id, job);
  const report = job.report || {};
  $("detail-title").textContent = title(job);
  const facts = [
    ["Outcome", labels[job.state] || job.state],
    ["Requested", new Date(job.created * 1000).toLocaleString()],
    ["Origin", origins[job.origin] || job.origin],
    ["Attempt", job.attempts],
    ["Output", job.output || "No published output"],
    [
      "Runtime",
      report.runtime
        ? `${report.runtime.backend} · ${report.runtime.compute_type || ""}`
        : "Not reported",
    ],
  ];
  if (report.audit) {
    const before = report.audit.before;
    facts.push(
      ["Audit decision", before.decision],
      ["Evidence", before.reason],
      ["Supported cues", `${before.supported_cues} / ${before.total_cues}`],
      [
        "Original p95 timing error",
        before.p95_error_seconds == null
          ? "Insufficient evidence"
          : `${before.p95_error_seconds.toFixed(2)} seconds`,
      ],
    );
    if (report.audit.after)
      facts.push([
        "Candidate p95 timing error",
        report.audit.after.p95_error_seconds == null
          ? "Insufficient evidence"
          : `${report.audit.after.p95_error_seconds.toFixed(2)} seconds`,
      ]);
  }
  if (report.plex_delivery)
    facts.push([
      "Plex delivery",
      `${report.plex_delivery.state} · ${report.plex_delivery.reason || ""}`,
    ]);
  $("detail-content").innerHTML =
    `${badge(job.state)}<p>${esc(job.media)}</p>${job.state === "processing" ? progressMarkup(job) : ""}${job.error ? `<div class="notice">${esc(job.error)}</div>` : ""}<dl class="detail-list">${facts.map(([name, value]) => `<dt>${name}</dt><dd>${esc(value)}</dd>`).join("")}</dl>${report.candidate ? `<div class="review-box"><h3>Review subtitle candidate</h3><p>Download this private candidate and check it against the video before publishing.</p><a class="button" href="/api/jobs/${id}/candidate" download>Download candidate</a>${job.state === "review" ? `<label class="check-field mt-18"><input type="checkbox" id="review-confirm"><span>I checked this candidate against the video.</span></label>${button("Publish reviewed candidate", "approve", "primary", `data-id="${id}" id="publish-candidate" disabled`)}` : ""}</div>` : ""}${(report.issues || []).length ? `<h3 class="mt-20">Quality findings</h3><ul>${report.issues.map((issue) => `<li>${esc(issue)}</li>`).join("")}</ul>` : ""}${report.warnings?.length ? `<p>${report.warnings.map(esc).join(" · ")}</p>` : ""}<div class="actions mt-22">${button("Download audit report", "download-report", "", `data-id="${id}"`)}${settings.bazarr.url ? button("Inspect Bazarr alternatives", "providers", "", `data-id="${id}"`) : ""}</div><div id="provider-results"></div>`;
  if (!$("detail-dialog").open) $("detail-dialog").showModal();
}
function download(data, name) {
  const url = URL.createObjectURL(
    new Blob([JSON.stringify(data, null, 2)], { type: "application/json" }),
  );
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
async function copy(value) {
  if (navigator.clipboard && window.isSecureContext) {
    await navigator.clipboard.writeText(value);
    return;
  }
  const input = document.createElement("textarea");
  input.value = value;
  document.body.append(input);
  input.select();
  const ok = document.execCommand("copy");
  input.remove();
  if (!ok)
    throw new Error("Copy unavailable. Reveal the key and copy it manually.");
}
async function perform(target) {
  const action = target.dataset.action,
    id = Number(target.dataset.id);
  target.disabled = true;
  try {
    if (action === "pause" || action === "scan") {
      const response = await api(`/${action}`, "POST");
      if (action === "pause") {
        settings.paused = response.paused;
        toast(
          response.paused
            ? "Queue paused. Active processing continues."
            : "Queue resumed.",
        );
      } else toast(response.message);
    } else if (["promote", "cancel", "retry", "approve"].includes(action)) {
      const response = await api(`/jobs/${id}/${action}`, "POST");
      toast(response.message);
      if (action === "approve") await detail(id);
    } else if (action === "details") await detail(id);
    else if (action === "audit" || action === "generate") {
      const item = media[Number(target.dataset.index)];
      const response = await api("/process", "POST", {
        media: item.path,
        directive: action === "generate" ? "generate" : "",
      });
      toast(`${response.message}. Job #${response.job_id}.`);
    } else if (action === "previous" || action === "next") {
      pageOffset += action === "next" ? 25 : -25;
      await loadList();
    } else if (action === "reload") await loadList();
    else if (action === "test") {
      const result = await api(
        `/connections/${target.dataset.name}/test`,
        "POST",
      );
      toast(result.message || `${target.dataset.name} connection passed.`);
    } else if (action === "reveal") {
      $("api-key").type =
        $("api-key").type === "password" ? "text" : "password";
      target.textContent = $("api-key").type === "password" ? "Show" : "Hide";
    } else if (action === "copy-key") {
      await copy(settings.api_key);
      toast("API key copied.");
    } else if (action === "download-report")
      download(jobs.get(id).report || {}, `crowbarr-audit-${id}.json`);
    else if (action === "providers") {
      const result = await api(`/jobs/${id}/providers`, "POST");
      $("provider-results").innerHTML =
        `<h3 class="mt-20">Bazarr alternatives</h3><pre>${esc(JSON.stringify(result, null, 2))}</pre>`;
    }
    await refresh();
    if (["promote", "cancel", "retry"].includes(action)) await loadList();
  } catch (error) {
    toast(error.message, true);
  } finally {
    if (target.isConnected) target.disabled = false;
  }
}
function setTheme(theme) {
  document.documentElement.dataset.theme = theme;
  try {
    localStorage.setItem("crowbarr-arr-theme", theme);
  } catch {}
  $("theme-button").textContent =
    `Switch to ${theme === "dark" ? "light" : "dark"} theme`;
}
async function showLogin() {
  session = false;
  settings = savedSettings = null;
  status = null;
  jobs.clear();
  media = [];
  draftDirty = dirty = false;
  $("page").replaceChildren();
  $("detail-content").replaceChildren();
  $("detail-dialog").close();
  $("workspace").hidden = true;
  $("login").hidden = false;
  try {
    setup = !(await api("/session")).configured;
  } catch {
    setup = false;
  }
  $("login-title").textContent = setup
    ? "Set up your workspace"
    : "Welcome back";
  $("login-description").textContent = setup
    ? "Create a local login. Use a password of at least 8 characters."
    : "Sign in to your subtitle workspace.";
  $("login-submit").textContent = setup ? "Create workspace" : "Sign in";
  $("login-form").elements.password.autocomplete = setup
    ? "new-password"
    : "current-password";
}
async function enter() {
  settings = await api("/settings");
  savedSettings = structuredClone(settings);
  session = true;
  $("login").hidden = true;
  $("workspace").hidden = false;
  await refresh();
  await navigate();
}
$("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  $("login-submit").disabled = true;
  $("login-error").textContent = "";
  try {
    await api(setup ? "/setup" : "/session", "POST", {
      username: form.elements.username.value,
      password: form.elements.password.value,
    });
    form.elements.password.value = "";
    await enter();
  } catch (error) {
    $("login-error").textContent = error.message;
  } finally {
    $("login-submit").disabled = false;
  }
});
document.addEventListener("click", (event) => {
  const target = event.target.closest("[data-action]");
  if (target) perform(target);
});
document.addEventListener("input", (event) => {
  if (event.target.closest("#settings-form") && event.target.name) {
    dirty = draftDirty = true;
    $("save-state").textContent = "Unsaved changes";
  }
  if (["library-query", "queue-query"].includes(event.target.id)) {
    listQuery = event.target.value;
    pageOffset = 0;
    listRevision++;
    clearTimeout(timer);
    timer = setTimeout(() => loadList(), 250);
  }
});
document.addEventListener("change", (event) => {
  if (event.target.id === "library-provider") {
    libraryProvider = event.target.value;
    pageOffset = 0;
    loadList();
  }
  if (event.target.id === "history-filter") {
    historyFilter = event.target.value;
    pageOffset = 0;
    loadList();
  }
  if (event.target.id === "theme-select") setTheme(event.target.value);
  if (event.target.id === "review-confirm")
    $("publish-candidate").disabled = !event.target.checked;
});
document.addEventListener("submit", async (event) => {
  if (event.target.id !== "settings-form") return;
  event.preventDefault();
  const submit = event.submitter;
  submit.disabled = true;
  try {
    captureSettings();
    settings = await api("/settings", "PUT", settings);
    savedSettings = structuredClone(settings);
    dirty = draftDirty = false;
    toast("Settings saved. Library reconciliation requested.");
    await navigate();
  } catch (error) {
    toast(error.message, true);
  } finally {
    if (submit.isConnected) submit.disabled = false;
  }
});
$("sign-out").addEventListener("click", async () => {
  try {
    await api("/session", "DELETE");
    await showLogin();
  } catch (error) {
    toast(error.message, true);
  }
});
$("theme-button").addEventListener("click", () =>
  setTheme(
    document.documentElement.dataset.theme === "dark" ? "light" : "dark",
  ),
);
$("menu").innerHTML = icon("menu");
$("search-icon").innerHTML = icon("search");
$("close-detail").innerHTML = icon("close");
$("menu").addEventListener("click", () => {
  $("workspace").classList.toggle("menu-open");
  $("menu").setAttribute(
    "aria-expanded",
    String($("workspace").classList.contains("menu-open")),
  );
});
$("close-detail").addEventListener("click", () => $("detail-dialog").close());
window.addEventListener("hashchange", () =>
  navigate().catch((error) => toast(error.message, true)),
);
window.addEventListener("beforeunload", (event) => {
  if (draftDirty) {
    event.preventDefault();
    event.returnValue = "";
  }
});
document.addEventListener("keydown", (event) => {
  if (
    event.key === "/" &&
    session &&
    !/INPUT|TEXTAREA|SELECT/.test(event.target.tagName) &&
    !$("detail-dialog").open
  ) {
    event.preventDefault();
    if (route === "library") $("library-query").focus();
    else {
      location.hash = "library";
      setTimeout(() => $("library-query")?.focus(), 50);
    }
  }
  if (event.key === "Escape") {
    $("workspace").classList.remove("menu-open");
    $("menu").setAttribute("aria-expanded", "false");
  }
});
try {
  setTheme(localStorage.getItem("crowbarr-arr-theme") || "light");
} catch {
  setTheme("light");
}
enter().catch(showLogin);
setInterval(refresh, 3000);
