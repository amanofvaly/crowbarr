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
  clock: '<circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" stroke-width="2"/><path d="M12 7v5l3 2" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>',
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
  skip: '<path d="M5 12h14"/><circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" stroke-width="2"/>',
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
  skipped: "Set aside",
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
let renderedKey = null;
function syncAttributes(current, wanted) {
  for (const attribute of [...current.attributes]) {
    if (!wanted.hasAttribute(attribute.name)) current.removeAttribute(attribute.name);
  }
  for (const attribute of [...wanted.attributes]) {
    if (current.getAttribute(attribute.name) !== attribute.value)
      current.setAttribute(attribute.name, attribute.value);
  }
}
function merge(current, draft) {
  // Walk both trees and touch only the elements that actually differ. An element whose
  // markup is unchanged is never replaced, so a selection, a focused control or a
  // scrolled container survives while the rest of the page updates around it.
  const wanted = [...draft.children];
  const existing = [...current.children];
  for (let i = 0; i < wanted.length; i++) {
    const next = wanted[i];
    const node = existing[i];
    if (!node) {
      current.append(next);
    } else if (node.tagName !== next.tagName) {
      node.replaceWith(next);
    } else if (node.outerHTML !== next.outerHTML) {
      if (node.children.length && next.children.length) {
        syncAttributes(node, next);
        merge(node, next);
      }
      else node.replaceWith(next);
    }
  }
  for (let i = current.children.length - 1; i >= wanted.length; i--) {
    current.children[i].remove();
  }
}
function paint(id, markup) {
  const node = typeof id === "string" ? $(id) : id;
  if (!node) return;
  if (node.dataset.key === markup) return;   // nothing differs at all
  node.dataset.key = markup;
  const draft = document.createElement(node.tagName);
  draft.innerHTML = markup;
  merge(node, draft);
}
let jobs = new Map(),
  media = [],
  total = 0;
const navigation = [
  ["dashboard", "Dashboard"],
  ["activity", "Activity"],
  ["library", "Library"],
  ["review", "Review"],
  ["history", "History"],
  ["settings", "Settings"],
  ["api", "API & Webhooks"],
];
function notification(heading, text, tone = "success") {
  const node = document.createElement("div");
  node.className = `notification ${tone}`;
  node.innerHTML = `<strong>${esc(heading)}</strong><span>${esc(text)}</span>`;
  $("notifications").append(node);
  while ($("notifications").children.length > 6)
    $("notifications").firstElementChild.remove();
  setTimeout(() => {
    node.classList.add("leaving");
    setTimeout(() => node.remove(), 260);
  }, 6500);
}
function toast(text, error = false, heading = error ? "Action failed" : "Done") {
  notification(heading, text, error ? "error" : "success");
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
      if (stream) { stream.close(); stream = null; }
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
  paint("page-actions", actions);
  return `<div class="page-heading"><div><h1>${name}</h1><p>${description}</p></div><div class="actions">${actions}</div></div>`;
}
function panel(name, body, extra = "", style = "") {
  return `<section class="panel ${style}"><div class="panel-header"><h2>${name}</h2>${extra}</div>${body}</section>`;
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
function cooldownMarkup() {
  const total = status?.wait_total;
  const until = status?.wait_until;
  if (!total || !until) return "";
  const left = Math.max(0, until - Date.now() / 1000);
  const done = Math.min(100, Math.max(0, ((total - left) / total) * 100));
  return `<div class="cooldown"><div class="progress-label"><span data-countdown="${until}" data-total="${total}">Resuming in ${Math.ceil(left)}s</span></div><div class="bar"><span data-fill style="width:${done}%"></span></div></div>`;
}
function budgetText(includeResume = true) {
  const budget = status?.background_budget;
  if (!budget) return "Unavailable";
  const used = Math.min(budget.used_seconds, budget.limit_seconds) / 60;
  const limit = budget.limit_seconds / 60;
  const resume = includeResume && budget.resume_at
    ? ` Background work can resume around ${new Date(budget.resume_at * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}.`
    : "";
  return `${used.toFixed(used < 10 ? 1 : 0)} of ${limit} minutes used in the rolling hour.${resume}`;
}
function budgetMarkup() {
  return `<p class="progress-context">${esc(budgetText())}</p>`;
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
  const stage = job.stage || "Recognizing dialogue";
  const sampled = stage.startsWith("Sampling existing subtitle");
  const escalated = stage.startsWith("Sample inconclusive");
  const compactStage = sampled
    ? "Sampling existing subtitle"
    : escalated
      ? "Full audit after inconclusive sample"
      : job.directive === "generate"
        ? "Generating fresh subtitle"
        : stage.startsWith("No usable subtitle")
          ? "Generating from full audio"
          : stage.startsWith("Auditing existing subtitle")
            ? "Full audit of existing subtitle"
            : stage;
  const explanation = sampled
    ? "checking up to 6 minutes across the beginning, middle, and end"
    : escalated
      ? "the initial sample did not provide enough evidence for a verdict"
      : job.directive === "generate"
        ? "a fresh subtitle was explicitly requested"
        : stage.startsWith("No usable subtitle")
          ? "no usable authored subtitle was found"
          : stage.startsWith("Auditing existing subtitle")
            ? "checking an existing authored subtitle"
            : "analyzing dialogue audio";
  const amount = `${duration(job.progress_current)} of ${duration(job.progress_total)} ${sampled ? "sampled" : "full"} audio`;
  return measured
    ? `<div class="${compact ? "inline-progress" : ""}"><div class="progress-label"><span>${esc(compact ? compactStage : stage)}</span><strong>${percent.toFixed(1)}%</strong></div><progress aria-label="${esc(stage)}: ${percent.toFixed(1)}% of audio analyzed" max="${job.progress_total}" value="${job.progress_current}"></progress><p class="progress-context">${amount}${compact ? "" : ` · ${explanation}`}</p></div>`
    : `<div class="stage-working">${icon("refresh")}<span>${esc(job.stage || "Preparing worker")}${job.cached === 1 ? " · reusing recognised audio" : job.cached === 0 ? " · listening for the first time" : ""}</span>${job.started ? `<em class="elapsed" data-since="${job.started}">${Math.round(Date.now() / 1000 - job.started)}s</em>` : ""}</div>`;
}
function jobButtons(job) {
  const id = `data-id="${job.id}"`;
  return `${button("Details", "details", "small", id)}${["queued", "waiting", "retry"].includes(job.state) ? button("Run next", "promote", "small", id) : ""}${["queued", "waiting", "retry", "processing"].includes(job.state) ? button(job.cancel_requested ? "Cancelling…" : "Cancel", "cancel", "small danger", `${id} ${job.cancel_requested ? "disabled" : ""}`) : ["failed", "review", "skipped", "unchanged", "completed"].includes(job.state) ? button("Retry", "retry", "small", id) : ""}`;
}
function activeJob() {
  const job = status?.jobs.find((j) => j.state === "processing");
  if (!job && status?.scan_in_progress)
    return `<div class="worker-band idle">${icon("refresh")}<div class="idle-body"><strong>Syncing libraries</strong><span>Crowbarr is checking which files need work. Queue totals can change until this finishes.</span></div></div>`;
  if (!job)
    return `<div class="worker-band idle">${icon(status?.paused ? "pause" : status?.wait_reason ? "clock" : "check")}<div class="idle-body"><strong>${status?.paused ? "Queue paused" : status?.wait_reason ? "Waiting to process" : "Worker idle"}</strong><span>${esc(status?.wait_reason || "No job is currently processing.")}</span>${status?.wait_reason === "Hourly background budget reached" ? budgetMarkup() : cooldownMarkup()}</div></div>`;
  return `<div class="worker-band"><div class="worker-media">${badge("processing")}<div class="active-title">${esc(title(job))}</div><span class="hint">${esc(origins[job.origin] || "Library sweep")} · ${job.started ? duration(Date.now() / 1000 - job.started) + " elapsed" : "Starting"}</span></div><div class="worker-progress">${progressMarkup(job)}</div><div class="actions">${jobButtons(job)}</div></div>`;
}

function rows(items, compact = false) {
  items.forEach((j) => jobs.set(j.id, j));
  const timeHeading = items.every((job) => FINISHED.includes(job.state))
    ? "Finished"
    : "Requested";
  return `<div class="table-wrap"><table><thead><tr><th>Media</th><th>Status</th>${compact ? "" : `<th>${timeHeading}</th>`}<th>Actions</th></tr></thead><tbody>${items.map((j) => `<tr><td class="title-cell"><button class="title-link" data-action="details" data-id="${j.id}">${esc(title(j))}</button><small>${esc(origins[j.origin] || "Library sweep")}${j.error ? ` · ${esc(j.error)}` : j.state === "waiting" ? ` · Eligible ${esc(new Date(j.ready * 1000).toLocaleString())}` : ""}</small></td><td>${badge(j.state)}${j.state === "processing" ? progressMarkup(j, true) : ""}</td>${compact ? "" : `<td class="muted">${esc(ago(FINISHED.includes(j.state) ? j.updated : j.created))}</td>`}<td><div class="row-actions">${compact ? button("Details", "details", "small", `data-id="${j.id}"`) : jobButtons(j)}</div></td></tr>`).join("")}</tbody></table></div>`;
}
function dashboardJobList(items, finished = false) {
  items.forEach((job) => jobs.set(job.id, job));
  return `<div class="dashboard-job-list">${items
    .map(
      (job) =>
        `<div class="dashboard-job"><div><button class="title-link" data-action="details" data-id="${job.id}">${esc(title(job))}</button><small>${esc(finished ? `${labels[job.state] || job.state} · ${ago(job.updated)}` : origins[job.origin] || "Library sweep")}</small></div><div>${badge(job.state)}${button("Details", "details", "small", `data-id="${job.id}"`)}</div></div>`,
    )
    .join("")}</div>`;
}
function systemStrip() {
  const r = status?.resources || {};
  const resources = [
    ["GPU", r.gpu || "Not detected"],
    [
      "GPU memory",
      r.vram_free_mb == null
        ? "Unavailable"
        : `${fmt(r.vram_free_mb)} / ${fmt(r.vram_total_mb)} MB free`,
    ],
    [
      "RAM available",
      r.ram_available_mb == null ? "Unavailable" : `${fmt(r.ram_available_mb)} MB`,
    ],
    ["Speech model", savedSettings?.model || "Unavailable"],
  ];
  return `<div class="system-strip" aria-label="System status">${resources
    .map(
      ([label, value]) =>
        `<span class="system-value"><span>${label}:</span> ${esc(value)}</span>`,
    )
    .join("")}${[
    "sonarr",
    "radarr",
    "bazarr",
    "plex",
  ]
    .map((name) => {
      const sync = status?.integrations?.find((item) => item.provider === name);
      const connected = Boolean(savedSettings?.[name]?.url);
      const state = !connected
        ? "Not configured"
        : sync?.healthy
          ? `${fmt(sync.file_count)} files`
          : sync
            ? "Sync unavailable"
            : "Configured";
      return `<a class="system-service" href="#settings/connections" title="${esc(state)}"><span class="status-dot ${!connected ? "neutral" : !sync?.healthy ? "warning" : ""}" aria-hidden="true"></span>${name[0].toUpperCase() + name.slice(1)}<span class="sr-only">: ${esc(state)}</span></a>`;
    })
    .join("")}<span class="system-value"><span>Background budget:</span> ${esc(budgetText(false))}</span><a href="#settings/resources">Resources</a></div>${status?.wait_reason ? `<p class="system-wait">${esc(status.wait_reason)}</p>` : ""}`;
}
function dashboard() {
  const counts = status?.counts || {};
  const pending = (status?.jobs || [])
    .filter((j) => ["queued", "waiting", "retry"].includes(j.state))
    .slice(0, 3);
  const recent = (status?.jobs || [])
    .filter(
      (j) => !["processing", "queued", "waiting", "retry"].includes(j.state),
    )
    .slice(0, 5);
  const waiting = ["queued", "waiting", "retry"].reduce(
    (sum, key) => sum + (counts[key] || 0),
    0,
  );
  return (
    heading(
      "Dashboard",
      "",
      button(
        icon(status?.paused ? "play" : "pause") +
          (status?.paused ? "Resume queue" : "Pause queue"),
        "pause",
      ) +
        button(icon("refresh") + "Sync libraries", "scan"),
    ) +
    (!status?.configured
      ? `<div class="callout"><div><strong>No libraries configured</strong><p>Connect a media manager or add your media folders to begin.</p></div>${link("Configure libraries", "settings/connections", "primary")}</div>`
      : "") +
    `<div class="overview-line"><span><strong>${fmt(status?.media_count)}</strong> media files</span><span><strong>${fmt(waiting)}</strong> pending</span><span><strong>${fmt(counts.completed)}</strong> subtitles written</span><span><strong>${fmt(counts.unchanged)}</strong> subtitles unchanged</span><a href="#review"><strong>${fmt((counts.review || 0) + (counts.failed || 0))}</strong> need review</a><span class="last-sync">Library sync: ${esc(ago(status?.last_scan))}</span></div>` +
    activeJob() +
    systemStrip() +
    `<div class="dashboard-split">${panel(
      "Recent outcomes",
      recent.length
        ? dashboardJobList(recent, true)
        : empty("No outcomes yet", "Finished jobs will appear here."),
      '<a href="#history">View history</a>',
    )}${panel(
      "Next in queue",
      pending.length
        ? dashboardJobList(pending)
        : empty("Queue is empty", "Imported media will be queued automatically."),
      `<a href="#activity">View all ${fmt(waiting)} pending jobs</a>`,
    )}</div>`
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
    `<section class="panel"><div class="queue-toolbar"><label class="sr-only" for="queue-query">Search jobs</label><input type="search" id="queue-query" placeholder="Filter by title or file path…" value="${esc(listQuery)}">${route === "history" ? `<label class="sr-only" for="history-filter">Filter outcomes</label><select id="history-filter">${["history", "completed", "unchanged", "review", "failed", "skipped", "cancelled", "superseded"].map((s) => `<option value="${s}" ${historyFilter === s ? "selected" : ""}>${s === "history" ? "All outcomes" : labels[s]}</option>`).join("")}</select>` : ""}<span class="hint">${route === "activity" ? "Manual requests run ahead of background work" : "Results from your server"}</span></div><div id="list-content" aria-live="polite"><div class="loading">Loading ${name.toLowerCase()}…</div></div></section>`
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
      const libraryMarkup = media.length
        ? `<div class="table-wrap"><table><thead><tr><th>Title / file</th><th>Source</th><th>Subtitle actions</th></tr></thead><tbody>${media.map((item, index) => `<tr><td class="title-cell"><strong>${esc(item.label !== item.path ? item.label : item.title)}</strong><small>${esc(item.path)}</small></td><td><span class="badge">${esc(item.provider === "folders" ? "Folders" : item.provider === "sonarr" ? "Sonarr" : "Radarr")}</span></td><td><div class="row-actions">${button("Audit subtitles", "audit", "small primary", `data-index="${index}"`)}${button("Generate fresh", "generate", "small", `data-index="${index}"`)}</div></td></tr>`).join("")}</tbody></table></div>${pager()}`
        : empty(
            listQuery ? "No matching media" : "Your library is waiting",
            listQuery
              ? "Try a shorter title, episode number, or another source."
              : "Connect your media managers or folders, then sync your library.",
            link("Manage libraries", "settings/connections"),
            "search",
          );
      paint("list-content", libraryMarkup);
    } else {
      const listMarkup = result.results.length
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
      paint("list-content", listMarkup);
    }
  } catch (error) {
    if (revision === listRevision && $("list-content") && !silent) {
      paint(
        "list-content",
        empty(
          "Couldn’t load this view",
          esc(error.message),
          button("Try again", "reload"),
          "review",
        ),
      );
    }
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
const check = (name, label, help = "", disabled = false) =>
  `<label class="check-field wide"><input name="${name}" type="checkbox" ${settings[name] ? "checked" : ""} ${disabled ? "disabled" : ""}><span>${label}<small>${help}</small></span></label>`;
const select = (name, label, options, help = "") =>
  `<label>${label}<select name="${name}" aria-describedby="${name}-hint">${options
    .map((value) => {
      const [id, text, disabled] = Array.isArray(value) ? value : [value, value];
      return `<option value="${esc(id)}" ${settings[name] === id ? "selected" : ""} ${disabled ? "disabled" : ""}>${esc(text)}</option>`;
    })
    .join("")}</select><small id="${name}-hint">${help}</small></label>`;
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
const SPEECH_MODELS = [
  ["tiny.en", "Tiny (English)", "75 MB"],
  ["base.en", "Base (English)", "145 MB"],
  ["small.en", "Small (English)", "480 MB"],
  ["medium.en", "Medium (English)", "1.5 GB"],
  ["large-v3-turbo", "Large v3 Turbo", "1.6 GB"],
  ["large-v3", "Large v3", "3 GB"],
];
// Multilingual builds stay valid so existing settings keep working, but they are not
// offered: Crowbarr is English only, and the .en build of a size is better at it.
const LEGACY_MODELS = {
  tiny: ["Tiny (multilingual)", "75 MB"],
  base: ["Base (multilingual)", "145 MB"],
  small: ["Small (multilingual)", "480 MB"],
  medium: ["Medium (multilingual)", "1.5 GB"],
};
function megabytes(size) {
  const [value, unit] = size.split(" ");
  return unit === "GB" ? Number(value) * 1024 : Number(value);
}
// The server owns the list of settings a verdict depends on. Changing one re-checks
// the library, which is a decision worth offering rather than performing silently.
function recheckFields() {
  return settings?.capabilities?.recheck_fields || [];
}
function saveActions() {
  const changed = recheckFields().some(
    (key) => savedSettings && settings[key] !== savedSettings[key],
  );
  if (!changed) return `<button class="button primary" type="submit">Save changes</button>`;
  const files = status?.media_count ? ` ${fmt(status.media_count)} files` : " the library";
  return (
    `<button class="button primary" type="submit">Save and re-check${files}</button>` +
    `<button class="button" type="submit" data-keep="true">Use for new work; keep finished results</button>`
  );
}
// One control per row, whose label is the state: Download, Select, Active. A model is
// selectable only once it is on disk, so no job ever waits on a download.
function assetTable(caption, rows, note) {
  const body = rows
    .map(
      ([label, size, action]) =>
        `<tr><td>${esc(label)}</td><td class="asset-size">${esc(size)}</td>` +
        `<td class="asset-state">${action}</td></tr>`,
    )
    .join("");
  return `<div class="wide asset-table"><table><caption>${esc(caption)}</caption><tbody>${body}</tbody></table>${
    note ? `<small>${esc(note)}</small>` : ""
  }</div>`;
}
function modelChooser(chosen, downloaded, fetching) {
  const listed = SPEECH_MODELS.some(([id]) => id === chosen)
    ? SPEECH_MODELS
    : [...SPEECH_MODELS, [chosen, ...(LEGACY_MODELS[chosen] || [chosen, ""])]];
  const vram = settings.device === "cuda" ? status?.resources?.vram_total_mb : 0;
  const rows = listed.map(([id, label, size]) => {
    const attrs = `data-model="${esc(id)}"`;
    if (Boolean(vram) && size && megabytes(size) > vram)
      return [label, size, button(`Needs ${Math.ceil(megabytes(size) / 1024)} GB`, "", "muted", "disabled")];
    if (id === chosen) return [label, size, button("Active", "", "primary", "disabled")];
    if (downloaded.has(id)) return [label, size, button("Select", "choose-model", "", attrs)];
    if (fetching[id]?.status === "running")
      return [label, size, button("Downloading", "", "muted", "disabled")];
    if (fetching[id]?.status === "failed")
      return [label, size, button("Retry", "fetch-model", "danger", attrs)];
    return [label, size, button("Download", "fetch-model", "", attrs)];
  });
  return assetTable("Speech model", rows, "Larger is more accurate and slower.");
}
function alignmentAsset(alignment, available) {
  if (!available) return "";
  const ready = alignment?.present === true;
  const size = ready ? `${Math.round((alignment.bytes || 0) / 1048576)} MB` : "360 MB";
  const action = ready
    ? button("Ready", "", "primary", "disabled")
    : alignment?.status === "running"
      ? button("Downloading", "", "muted", "disabled")
      : button(alignment?.status === "failed" ? "Retry" : "Download", "fetch-alignment", "");
  return assetTable("Refinement model", [["WhisperX alignment", size, action]]);
}
function refreshSaveActions() {
  const bar = document.querySelector(".save-bar");
  if (!bar) return;
  captureSettings();
  const state = $("save-state");
  bar.innerHTML = saveActions();
  bar.append(state);
}
function settingsPage() {
  const capabilities = settings.capabilities;
  const recheckCount = status?.media_count
    ? `all ${fmt(status.media_count)} media files`
    : "every media file";
  const cpuAvailable = capabilities?.cpu?.available === true;
  const cudaAvailable = capabilities?.cuda?.available === true;
  const refinementAvailable = capabilities?.refinement?.available === true;
  const downloaded = new Set(capabilities?.models?.speech || []);
  const fetching = capabilities?.models?.speech_downloads || {};
  const alignment = capabilities?.models?.alignment;
  const downloading = alignment?.status === "running";
  // Available needs no explanation; the table shows size and state. Unavailable does.
  const refinementHelp = refinementAvailable
    ? "Runs on the CPU."
    : capabilities?.refinement?.reason || "Availability could not be determined.";
  const alignmentAction = alignmentAsset(alignment, refinementAvailable);
  const deviceHelp = [
    !cpuAvailable ? capabilities?.cpu?.reason || "CPU runtime availability could not be determined." : "",
    cudaAvailable
      ? "An NVIDIA graphics card was found. Loading the model can still fail if its memory runs short."
      : capabilities?.cuda?.reason || "CUDA availability could not be determined.",
    settings.device === "cuda" && !cudaAvailable
      ? settings.cpu_fallback
        ? "Crowbarr is set to use a graphics card, but none is available. Processing runs on the CPU instead."
        : "Crowbarr is set to use a graphics card, but none is available. Turn on CPU fallback or choose CPU, or jobs will not run."
      : "",
  ].filter(Boolean).join(" ");
  let content = "";
  if (section === "connections") content = connectionSettings();
  if (section === "library")
    content = settingPanel(
      "Media & discovery",
      status?.discovery_mode === "arr"
        ? "Your library comes from Sonarr/Radarr. These folders tell Crowbarr where it can access the videos and write subtitles. Plex does not supply the library."
        : "No media manager is connected, so Crowbarr scans these folders for media itself.",
      `<label class="wide">Media folders<textarea name="roots" rows="4" placeholder="/media/tv&#10;/media/movies">${esc(settings.roots.join("\n"))}</textarea><small>Enter one folder path per line, or use Find my media folders. You can edit the paths before saving.</small></label><div class="wide actions">${button("Find my media folders", "suggest-folders")}${button("Test folders", "test-folders")}</div><div class="wide" id="folder-results" role="status" aria-live="polite"></div><details class="wide folder-help"><summary>Where do I find this path?</summary><p>Use the folder path as Crowbarr sees it. This box selects a folder; it does not share a folder with the app.</p><ul><li><strong>Docker Compose:</strong> Open Crowbarr’s compose file and look under <code>volumes</code>. For <code>/mnt/pool/TV:/tv</code>, enter <code>/tv</code>. If your library is missing, add its folder there and recreate the Crowbarr container.</li><li><strong>TrueNAS:</strong> Open Apps, select Crowbarr, then Edit. Under Storage, find your media folder and copy its Mount Path. If it is missing, add storage for that folder, choose a Mount Path such as <code>/tv</code>, allow writes, and save the app.</li><li><strong>Native install:</strong> Copy the full path of your media folder on the computer running Crowbarr, such as <code>/srv/media/tv</code>.</li></ul><p>Find uses saved Sonarr/Radarr connections and shows the paths they report. You can also find those paths in Sonarr/Radarr → Settings → Media Management → Root Folders.</p><p>If a manager uses a different path for the same folder, open <a href="#settings/connections">Settings → Connections</a>. In that manager’s Path mappings box, enter its path on the left and Crowbarr’s path on the right, for example <code>/data/tv => /tv</code>. Save, then find and test again.</p></details>${numeric("scan_seconds", "Sync interval (seconds)", 10, 86400, "How often Crowbarr looks for new or changed media files.")}${numeric("settle_seconds", "File settling time (seconds)", 0, 86400, "Wait for files to stop changing before processing.")}${numeric("subtitle_wait_minutes", "Wait for Bazarr (minutes)", 0, 10080, "How long to wait for Bazarr to supply a subtitle. After that, Crowbarr checks subtitles inside the video file, then writes its own.")}`,
    );
  if (section === "processing")
    content = settingPanel(
      "Speech processing",
      `English only. Changing the model, device or precision re-checks ${recheckCount}.`,
      modelChooser(settings.model, downloaded, fetching) +
        select("device", "Processing device", [
          ["cpu", cpuAvailable ? "CPU" : "CPU (unavailable)", !cpuAvailable],
          ["cuda", cudaAvailable ? "NVIDIA GPU · CUDA" : "NVIDIA GPU · CUDA (unavailable)", !cudaAvailable],
        ], `<span id="device-help" aria-live="polite">${esc(deviceHelp)}</span>`) +
        select(
          "compute_type",
          "Compute precision",
          ["int8", "float32", "int8_float16", "float16"].map((type) => [
            type,
            {
              int8: "INT8 (default, lowest memory)",
              float32: "FP32 (full precision, most memory)",
              int8_float16: "INT8 and FP16 (graphics card only)",
              float16: "FP16 (graphics card only)",
            }[type],
            settings.device === "cpu" && type.includes("float16"),
          ]),
          '<span id="precision-help" aria-live="polite">Lower precision uses less memory and runs faster, with a small loss of accuracy. The CPU supports INT8 and FP32 only.</span>',
        ) +
        numeric(
          "cpu_threads",
          "CPU threads",
          1,
          64,
          "Used for CPU processing and for refinement. Ignored while recognition runs on the GPU. Setting it above the machine's core count makes jobs slower.",
        ) +
        check(
          "cpu_fallback",
          "Fall back to CPU",
          "Process on the CPU when the graphics card cannot be used. Jobs may take longer.",
        ) +
        check(
          "generate_over_mismatch",
          "Replace a subtitle that cannot be repaired",
          "When the audio proves a subtitle is for other content, or for a shorter cut of this episode, write a fresh one instead of asking you to look. The rejected file is left on disk.",
        ) +
        check(
          "refine_generated",
          refinementAvailable ? "Refine generated timings with WhisperX" : "Refine generated timings with WhisperX (unavailable)",
          esc(refinementHelp),
          !refinementAvailable || !alignment?.present,
        ) +
        alignmentAction +
        check(
          "allow_untagged_subtitles",
          "Also check subtitles with no language label",
          "Crowbarr checks English subtitles. Files named Episode.srt, and tracks inside the video with no language tag, are skipped unless you turn this on. A subtitle that turns out to be another language will be checked against English audio and reported as not matching.",
        ),
    );
  if (section === "resources")
    content =
      settingPanel(
        "Resource limits",
        "Crowbarr processes one job at a time. These limits decide when a job may start.",
        numeric(
          "min_free_ram_mb",
          "Minimum RAM headroom (MB)",
          128,
          262144,
          "Hold a job when the machine has less free memory than this.",
        ) +
          numeric(
            "min_free_vram_mb",
            "Minimum free GPU memory (MB)",
            128,
            262144,
            "Hold a job when the graphics card has less free memory than this. Ignored when processing on the CPU.",
          ) +
          numeric(
            "max_cpu_load",
            "Maximum background CPU load per core",
            0.1,
            4,
            "Hold background jobs when the machine is busier than this. 1 means every CPU core already has work.",
            // A step of 0.1 rejects the shipped 0.75 default and blocks the whole panel.
            "any",
          ),
      ) +
      settingPanel(
        "Background schedule",
        "These limits apply to background work only. Jobs you start yourself, and jobs from a new import, ignore them.",
        numeric(
          "backlog_cooldown_seconds",
          "Cooldown between jobs (seconds)",
          0,
          86400,
          "Wait this long after a background job before starting the next one.",
        ) +
          numeric(
            "background_budget_minutes",
            "Processing budget per hour (minutes)",
            1,
            60,
            "Minutes in each hour that background jobs may run.",
          ) +
          select(
            "quiet_hour_start",
            "Quiet hours start",
            Array.from({ length: 24 }, (_, hour) => [hour, `${String(hour).padStart(2, "0")}:00`]),
            "24-hour time in the server’s timezone. Background work pauses during quiet hours.",
          ) +
          select(
            "quiet_hour_end",
            "Quiet hours end",
            Array.from({ length: 24 }, (_, hour) => [hour, `${String(hour).padStart(2, "0")}:00`]),
            "May end the following day (e.g. 22:00–07:00). Set both times equal to disable quiet hours.",
          ) +
          check(
            "defer_during_plex",
            "Defer background processing during Plex playback",
          ),
      );
  if (section === "quality")
    content = settingPanel(
      "Audit & recovery",
      `An authored subtitle is one a person wrote. These settings decide how closely it must match what is spoken. Changing a threshold re-checks ${recheckCount}.`,
      check(
        "sampled_audit",
        "Check short samples first",
        "Listen to samples spread through the file, and recognize the whole file only when the samples are unclear. Applies to media longer than ten minutes.",
      ) +
        check(
          "bazarr_download_alternatives",
          "Download alternatives from Bazarr",
          "Ask Bazarr for other subtitle files when the current one does not match the audio.",
        ) +
        numeric(
          "max_provider_attempts",
          "Subtitles to try per file",
          1,
          10,
          "How many subtitles Crowbarr downloads from Bazarr for one file before giving up.",
        ) +
        numeric(
          "max_attempts",
          "Maximum job attempts",
          1,
          10,
          "How many times a job is retried before it is marked failed.",
        ) +
        numeric(
          "job_timeout_minutes",
          "Job timeout (minutes)",
          1,
          1440,
          "Stop a job that has been running longer than this.",
        ) +
        numeric(
          "min_match_ratio",
          "Minimum subtitle match",
          0.5,
          1,
          "How much of an authored subtitle's text must match the audio. Below this, Crowbarr treats the subtitle as belonging to other content.",
          0.01,
        ) +
        numeric(
          "min_alignment_score",
          "Minimum timing confidence",
          0,
          1,
          "How sure the word timing must be. Below this, Crowbarr keeps Whisper's own timings instead.",
          0.01,
        ) +
        numeric(
          "max_generated_ratio",
          "Maximum unmatched dialogue",
          0,
          1,
          "How much spoken dialogue may fall outside an authored subtitle's lines before Crowbarr warns that some timings were estimated.",
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
    `<div class="settings-layout"><nav class="settings-nav" aria-label="Settings sections">${groups.map(([id, label]) => `<a href="#settings/${id}" class="nav-link ${id === section ? "active" : ""}" ${id === section ? 'aria-current="page"' : ""}>${label}</a>`).join("")}</nav><form id="settings-form" class="settings-form">${content}${section !== "appearance" ? `<div class="save-bar">${saveActions()}<span id="save-state" class="settings-status">All changes saved</span></div>` : ""}</form></div>`
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
    [
      "POST",
      "/api/jobs/{id}/skip",
      "Set aside an unresolved result; policy upgrades will not reopen it.",
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
  renderedKey = null;
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
    // Every radio in a group carries a name and a value, so reading the unchecked ones
    // would overwrite the chosen value with whichever appears last in the form.
    if (el.type === "radio" && !el.checked) continue;
    const [name, key] = el.name.split(".");
    const value =
      el.type === "checkbox"
        ? el.checked
        : el.type === "number" || ["quiet_hour_start", "quiet_hour_end"].includes(el.name)
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
function dashboardKey() {
  // Everything that changes the markup. Elapsed seconds and the cooldown countdown are
  // deliberately absent: the browser ticks those in place, so they must not force the
  // page to be rebuilt three times a minute.
  const c = status?.counts || {};
  return JSON.stringify([
    status?.paused,
    status?.wait_reason,
    status?.background_budget,
    status?.media_count,
    status?.last_scan,
    status?.audit_policy_version,
    status?.resources,
    status?.integrations,
    savedSettings?.model,
    Object.keys(c).sort().map((k) => [k, c[k]]),
    (status?.jobs || []).map((j) => [j.id, j.state, j.stage, j.error, j.priority, j.origin, j.progress_current, j.progress_total]),
    (status?.notices || []).length,
  ]);
}
function renderDashboard() {
  const key = dashboardKey();
  if (key === renderedKey) return;   // nothing changed; leave the DOM alone
  renderedKey = key;
  const focused = document.activeElement?.closest("[data-action]");
  const focusAction = focused?.dataset.action,
    focusId = focused?.dataset.id;
  const previous = $("page").querySelector("progress")?.value;
  const oldTitle = $("page").querySelector(".active-title")?.textContent;
  paint("page", dashboard());
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
const TERMINAL = ["completed", "unchanged", "review", "failed"];
const FINISHED = ["completed", "unchanged", "review", "failed", "skipped", "superseded", "cancelled"];
const verdicts = {
  completed: "Subtitle written",
  unchanged: "Already correct",
  review: "Needs attention",
  failed: "Failed",
  skipped: "Set aside",
};
let announced = null;
function verdictToast(job) {
  notification(
    verdicts[job.state] || job.state,
    title(job),
    ["review", "failed"].includes(job.state) ? "warning" : "success",
  );
}
function announce(list) {
  // First payload establishes what is already known; only later changes are news.
  const current = new Map(list.map((job) => [job.id, job.state]));
  if (announced === null) return void (announced = current);
  for (const job of list) {
    if (TERMINAL.includes(job.state) && announced.get(job.id) !== job.state) verdictToast(job);
  }
  announced = current;
}
function tick() {
  const now = Date.now() / 1000;
  for (const node of document.querySelectorAll("[data-since]")) {
    node.textContent = `${Math.round(now - Number(node.dataset.since))}s`;
  }
  for (const node of document.querySelectorAll("[data-countdown]")) {
    const total = Number(node.dataset.total) || 1;
    const left = Math.max(0, Number(node.dataset.countdown) - now);
    node.textContent = left > 0 ? `Resuming in ${Math.ceil(left)}s` : "Starting next file";
    const done = Math.min(100, Math.max(0, ((total - left) / total) * 100));
    const bar = node.closest(".cooldown")?.querySelector("[data-fill]");
    if (bar) bar.style.width = `${done}%`;
  }
}
setInterval(tick, 250);
function apply(next) {
  // Everything the shell shows is written value by value. The shell itself is rendered
  // once at sign-in and never rebuilt.
  if (!session) return;
  status = next;
  status.jobs.forEach((j) => jobs.set(j.id, j));
  announce(status.jobs);
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
  $("version").textContent = `v${status.version}`;
  $("audit-policy-version").textContent =
    `v${status.audit_policy_version}`;
  drawNav();
  const notices = (status.notices || []).map((n) => n.message);
  if (!status.ffmpeg)
    notices.push("FFmpeg is unavailable. Install FFmpeg and ffprobe to process media.");
  const text = notices.join(" · ");
  if ($("notice").textContent !== text) $("notice").textContent = text;
  $("notice").hidden = !notices.length;
  if (route === "dashboard" && !$("detail-dialog").open) renderDashboard();
  else if (
    ["activity", "review", "history"].includes(route) &&
    !$("detail-dialog").open &&
    !document.activeElement?.matches("input,select,button:disabled")
  )
    loadList(true);
}
async function refresh() {
  // Used for the first paint and after an action; the stream carries everything after.
  if (!session) return;
  try {
    apply(await api("/status"));
  } catch (error) {
    $("connection-status").textContent = "Connection interrupted";
    $("notice").textContent = `Live updates unavailable. ${error.message}`;
    $("notice").hidden = false;
  }
}
function measureTable(list, caption) {
  if (!list || !list.length) return "";
  const rows = list
    .map(
      (check) =>
        `<tr class="${check.passed ? "within" : "over"}"><td>${esc(check.name)}</td><td>${esc(check.measured)}</td><td>${esc(check.limit)}</td></tr>`,
    )
    .join("");
  return `<table class="measure"><caption>${esc(caption)}</caption><thead><tr><th>Measured</th><th>Result</th><th>Limit</th></tr></thead><tbody>${rows}</tbody></table>`;
}
async function detail(id) {
  const job = await api(`/jobs/${id}`);
  jobs.set(job.id, job);
  const report = job.report || {};
  const warnings = report.warnings || [];
  const legacyEscalation =
    report.mode === "authored_timing" &&
    warnings.some((warning) => warning.startsWith("Sample ")) &&
    warnings.some((warning) => warning.startsWith("Chunk "));
  const fullAuditEscalation =
    report.full_audit_escalation === true || legacyEscalation;
  const sampledSeconds = (report.initial_sampling_windows || []).reduce(
    (sum, [start, end]) => sum + Math.max(0, end - start),
    0,
  );
  const audioAnalysis = fullAuditEscalation
    ? "Full audio — the initial sample was inconclusive"
    : report.mode === "generated"
      ? "Full audio — required to generate a fresh subtitle"
      : sampledSeconds
        ? `${duration(sampledSeconds)} sampled across the runtime`
        : report.mode === "authored_timing"
          ? "Full audio used to audit the existing subtitle"
          : null;
  $("detail-title").textContent = title(job);
  const facts = [
    ["Outcome", labels[job.state] || job.state],
    [
      "Requested work",
      job.directive === "generate"
        ? "Generate a fresh subtitle from the full audio"
        : "Audit an existing subtitle and repair it only when needed",
    ],
    ...(audioAnalysis ? [["Audio analysis", audioAnalysis]] : []),
    ["Requested", new Date(job.created * 1000).toLocaleString()],
    ...(FINISHED.includes(job.state)
      ? [["Finished", new Date(job.updated * 1000).toLocaleString()]]
      : []),
    ["Origin", origins[job.origin] || job.origin],
    ["Attempt", job.attempts],
    ["Output", job.output || "No published output"],
    ...(report.selected_source
      ? [
          [
            "Audited subtitle",
            `${report.selected_source}${report.selected_source_kind ? ` (${report.selected_source_kind})` : ""}`,
          ],
        ]
      : []),
    ...(report.audio_selection
      ? [
          [
            "Audio track",
            `Stream ${report.audio_selection.stream} · ${report.audio_selection.channels} channels · tagged ${report.audio_selection.language_tag}`,
          ],
        ]
      : []),
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
      [
        "Audit decision",
        before.decision === "pass"
          ? "Passed — original subtitle retained"
          : before.decision === "repair"
            ? "Repair needed"
            : before.decision === "mismatched"
              ? "Wrong content — this subtitle is not this recording"
              : before.decision === "different_cut"
                ? "Different cut — written for a shorter version of this episode"
                : "Inconclusive — review needed",
      ],
      ["Evidence", before.reason],
      ["Supported cues", `${before.supported_cues} / ${before.total_cues}`],
      ...(before.recognized_words != null
        ? [
            [
              "Recognition quality",
              `${fmt(before.recognized_words)} words recognized · ${Math.round(
                before.low_confidence_ratio * 100,
              )}% of them low confidence`,
            ],
          ]
        : []),
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
  const auditExplanation = report.audit
    ? (() => {
        const result = report.audit.before;
        const verdict = report.audit.improvement;
        const matched = Number.isFinite(result.matched_token_ratio)
          ? `${Math.round(result.matched_token_ratio * 100)}% of recognized dialogue matched the subtitle text`
          : "The subtitle text matched the recognized dialogue";
        const distribution = result.distributed_across_timeline
          ? " across the beginning, middle, and end"
          : "";
        const anchors = `Crowbarr found ${fmt(result.supported_cues)} confident timing anchors${distribution}; ${esc(matched)}.`;
        const flagged = esc(
          result.reason.charAt(0).toLowerCase() + result.reason.slice(1),
        );
        const original = measureTable(result.checks, "The original subtitle");
        const untidy = (result.structural_issues || []).filter(
          (issue) => !issue.includes("unsuitable reading duration"),
        );
        if (result.decision === "pass")
          return `<section class="audit-explanation pass"><h3>Why the original was retained</h3><p>${esc(result.reason)}. ${anchors} Weak recognition regions were excluded from this evidence.${untidy.length ? ` Crowbarr also noted ${untidy.length === 1 ? "one line" : `${untidy.length} lines`} it would have written differently — ${esc(untidy.slice(0, 3).join("; "))}${untidy.length > 3 ? ", and others" : ""}. Timing is what this audit measures, and none of those change it.` : ""}</p>${original}</section>`;
        // Jobs audited before verdicts carried their reasoning still report `improved`,
        // so fall back to it rather than describing a repair that never shipped.
        const withheld = verdict
          ? !verdict.accepted
          : report.audit.after && report.audit.improved === false;
        if (result.decision === "repair" && withheld)
          return `<section class="audit-explanation warning"><h3>Why the repair was withheld</h3><p>The subtitle was flagged because ${flagged}. ${anchors} Crowbarr built a corrected version and measured it against those same anchors, then discarded it${verdict ? ` because ${esc(verdict.reason)}` : " because it did not improve the timing enough to justify replacing the original"}. Your original subtitle is untouched.</p>${original}${verdict ? measureTable(verdict.checks, "The repair Crowbarr built and rejected") : ""}</section>`;
        if (result.decision === "repair")
          return `<section class="audit-explanation"><h3>Why Crowbarr repaired the timing</h3><p>${esc(result.reason)}. ${anchors} The authored text is preserved while its cue timing is adjusted.</p>${original}${verdict ? measureTable(verdict.checks, "The repair Crowbarr accepted") : ""}</section>`;
        if (result.decision === "different_cut") {
          const replaced = report.replaced_source;
          const name = replaced ? esc(String(replaced).split("/").pop()) : "";
          return `<section class="audit-explanation warning"><h3>${replaced ? "Why the subtitle was replaced" : "Why this subtitle cannot be repaired"}</h3><p>${esc(result.reason)}. A repair shifts or stretches every line by one rule, so it can move a subtitle that is uniformly late — it cannot put back scenes the subtitle never had lines for. ${replaced ? `Crowbarr generated a fresh subtitle covering the whole recording and published it alongside <code>${name}</code>, which is untouched on disk.` : "Automatic replacement is switched off, so nothing was changed."}</p>${measureTable(result.cut_checks, "What showed the cut differs")}</section>`;
        }
        if (result.decision === "mismatched") {
          const replaced = report.replaced_source;
          const name = replaced ? esc(String(replaced).split("/").pop()) : "";
          return `<section class="audit-explanation warning"><h3>${replaced ? "Why the subtitle was replaced" : "Why this subtitle was rejected"}</h3><p>${esc(result.reason)}. This is not a timing fault: a subtitle minutes out of sync still shares its words with the dialogue, and this one shares almost none. ${replaced ? `Crowbarr generated a fresh subtitle from the speech it recognized and published it alongside <code>${name}</code>, which is untouched on disk.` : "Automatic replacement is switched off, so nothing was changed."}</p>${measureTable(result.mismatch_checks, "What proved the subtitle wrong")}</section>`;
        }
        return `<section class="audit-explanation warning"><h3>Why this needs review</h3><p>${esc(result.reason)}. ${anchors} Without that, Crowbarr cannot tell whether the timing is right, so your subtitle was left exactly as it was.</p>${measureTable(result.coverage_checks, "What the evidence had to clear")}</section>`;
      })()
    : "";
  $("detail-content").innerHTML =
    `${badge(job.state)}<p>${esc(job.media)}</p>${job.state === "processing" ? progressMarkup(job) : ""}${job.error ? `<div class="notice">${esc(job.error)}</div>` : ""}${auditExplanation}<dl class="detail-list">${facts.map(([name, value]) => `<dt>${name}</dt><dd>${esc(value)}</dd>`).join("")}</dl>${report.candidate ? `<div class="review-box"><h3>Review subtitle candidate</h3><p>Download this private candidate and check it against the video before publishing.</p><a class="button" href="/api/jobs/${id}/candidate" download>Download candidate</a>${job.state === "review" ? `<label class="check-field mt-18"><input type="checkbox" id="review-confirm"><span>I checked this candidate against the video.</span></label>${button("Publish reviewed candidate", "approve", "primary", `data-id="${id}" id="publish-candidate" disabled`)}` : ""}</div>` : ""}${(report.issues || []).length ? `<h3 class="mt-20">Quality findings</h3><ul>${report.issues.map((issue) => `<li>${esc(issue)}</li>`).join("")}</ul>` : ""}${warnings.length ? `<section class="recognition-notes"><h3>Recognition regions excluded from the verdict</h3><p>Crowbarr ignored these weak regions when forming timing evidence. They remain here so you can inspect what the recognizer encountered.</p><ul>${warnings.map((warning) => `<li>${esc(warning)}</li>`).join("")}</ul></section>` : ""}<div class="actions mt-22">${["review", "failed"].includes(job.state) ? `${button("Generate a fresh subtitle", "regenerate", "primary", `data-id="${id}"`)}${button("Set aside", "skip", "", `data-id="${id}"`)}` : ""}${button("Download audit report", "download-report", "", `data-id="${id}"`)}${settings.bazarr.url ? button("Inspect Bazarr alternatives", "providers", "", `data-id="${id}"`) : ""}</div><div id="provider-results"></div>`;
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
let modelPoll = null;
async function pollModels() {
  clearInterval(modelPoll);
  modelPoll = setInterval(async () => {
    const state = await api("/capabilities");
    settings.capabilities = state;
    const busy = Object.values(state.models?.speech_downloads || {}).some(
      (entry) => entry.status === "running",
    );
    if (!busy) {
      clearInterval(modelPoll);
      modelPoll = null;
      await navigate();
    }
  }, 5000);
}
let alignmentPoll = null;
async function pollAlignment() {
  clearInterval(alignmentPoll);
  alignmentPoll = setInterval(async () => {
    const state = await api("/capabilities");
    settings.capabilities = state;
    const model = state.models?.alignment;
    if (model?.status !== "running") {
      clearInterval(alignmentPoll);
      alignmentPoll = null;
      toast(model?.present ? "Alignment model is ready." : model?.reason || "The download did not finish.");
      await navigate();
    }
  }, 5000);
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
    } else if (["promote", "cancel", "retry", "approve", "skip"].includes(action)) {
      const response = await api(`/jobs/${id}/${action}`, "POST");
      toast(response.message);
      if (["approve", "skip"].includes(action)) {
        await detail(id);
        await loadList();
      }
    } else if (action === "regenerate") {
      const response = await api("/process", "POST", {
        media: jobs.get(Number(id)).media,
        directive: "generate",
      });
      toast(
        `Crowbarr will transcribe the audio and write a new subtitle. Job #${response.job_id}.`,
        false,
        "Fresh generation queued",
      );
      $("detail-dialog").close();
    } else if (action === "suggest-folders" || action === "test-folders") {
      const box = document.querySelector("#settings-form textarea[name=roots]");
      const output = $("folder-results");
      const value = box.value;
      delete output.dataset.tested;
      output.textContent = action === "suggest-folders" ? "Looking for folders using saved connections…" : "Testing folder access…";
      try {
        if (action === "test-folders") {
          const roots = value.split("\n").map((line) => line.trim()).filter(Boolean);
          if (!roots.length) {
            output.textContent = "Enter a folder or use Find my media folders first.";
            return;
          }
          const result = await api("/media-folders/test", "POST", { roots });
          if (!output.isConnected || box.value !== value) return;
          output.dataset.tested = "true";
          output.innerHTML = result.results.map((row) => `<p><strong>${esc(row.path)}: ${row.ok ? "Passed" : "Needs attention"}</strong><br>${esc(row.message)}</p>`).join("") + "<p>These results apply to the paths above. Save changes when you are ready.</p>";
        } else {
          const found = await api("/media-folders");
          if (!output.isConnected) return;
          const existing = box.value.split("\n").map((line) => line.trim()).filter(Boolean);
          box.value = [...new Set([...existing, ...found.paths])].join("\n");
          if (found.paths.length) box.dispatchEvent(new Event("input", { bubbles: true }));
          output.innerHTML = found.folders.map((row) => `<p><strong>${esc(row.provider === "sonarr" ? "Sonarr" : "Radarr")}</strong>: <code>${esc(row.remote)}</code>${row.path && row.path !== row.remote ? ` → <code>${esc(row.path)}</code>` : ""}<br>${row.accessible ? "Added to the box. Test folders to check write access." : `Crowbarr cannot reach this folder. Check its storage using the instructions below. If the same folder has a different path here, add a mapping in <a href="#settings/connections">Settings → Connections → ${esc(row.provider === "sonarr" ? "Sonarr" : "Radarr")} → Path mappings</a>.`}</p>`).join("") + found.errors.map((error) => `<p>${esc(error)}</p>`).join("") + (found.mounts.length ? `<p>Folders shared with Crowbarr. Choose the ones containing your videos:</p><div class="actions">${found.mounts.map((path) => button(`Add ${esc(path)}`, "add-folder", "", `data-path="${esc(path)}"`)).join("")}</div>` : "") + (!found.folders.length && !found.mounts.length ? "<p>No folders found. Enter a path using the instructions below, then test it.</p>" : "");
        }
      } catch (error) {
        if (output.isConnected) output.textContent = error.message;
      }
    } else if (action === "add-folder") {
      const box = document.querySelector("#settings-form textarea[name=roots]");
      box.value = [...new Set([...box.value.split("\n").map((line) => line.trim()).filter(Boolean), target.dataset.path])].join("\n");
      box.dispatchEvent(new Event("input", { bubbles: true }));
      target.textContent = `Added ${target.dataset.path}`;
      target.disabled = true;
    } else if (action === "choose-model") {
      // No radio exists now, so the choice lives in settings until it is saved.
      settings.model = target.dataset.model;
      dirty = draftDirty = true;
      await navigate();
      $("save-state").textContent = "Unsaved changes";
    } else if (action === "fetch-model") {
      const name = target.dataset.model;
      settings.capabilities = await api(`/capabilities/model/${encodeURIComponent(name)}`, "POST");
      toast(`Downloading ${name}.`);
      await navigate();
      pollModels();
    } else if (action === "fetch-alignment") {
      const response = await api("/capabilities/alignment", "POST");
      settings.capabilities = response;
      toast(
        response.models?.alignment?.present
          ? "Alignment model is ready."
          : "Downloading the alignment model. This can take a few minutes.",
      );
      await navigate();
      if (!response.models?.alignment?.present) pollAlignment();
    } else if (action === "details") await detail(id);
    else if (action === "audit" || action === "generate") {
      const item = media[Number(target.dataset.index)];
      const response = await api("/process", "POST", {
        media: item.path,
        directive: action === "generate" ? "generate" : "",
      });
      toast(
        `${item.label !== item.path ? item.label : item.title} was added to the priority queue. Manual requests run ahead of background work. Job #${response.job_id}.`,
        false,
        action === "generate" ? "Fresh generation queued" : "Subtitle audit queued",
      );
    } else if (action === "previous" || action === "next") {
      pageOffset += action === "next" ? 25 : -25;
      await loadList();
    } else if (action === "reload") await loadList();
    else if (action === "test") {
      const result = await api(
        `/connections/${target.dataset.name}/test`,
        "POST",
      );
      toast(result.message || `${target.dataset.name} connection passed.`, result.ok === false);
    } else if (action === "reveal") {
      $("api-key").type =
        $("api-key").type === "password" ? "text" : "password";
      target.textContent = $("api-key").type === "password" ? "Show" : "Hide";
    } else if (action === "copy-key") {
      await copy(settings.api_key);
      toast("API key copied.");
    } else if (action === "download-report")
      download(
        (jobs.get(id)?.report) || (await api(`/jobs/${id}`)).report || {},
        `crowbarr-audit-${id}.json`,
      );
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
  $("theme-choice").textContent =
    `Use ${theme === "dark" ? "light" : "dark"} theme`;
}
async function showLogin() {
  session = false;
  if (stream) { stream.close(); stream = null; }
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
  listen();          // from here the server pushes; nothing polls
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
    if (event.target.name === "roots" && $("folder-results")?.dataset.tested) {
      $("folder-results").textContent = "Folders changed. Test again to check these paths.";
    }
    dirty = draftDirty = true;
    $("save-state").textContent = "Unsaved changes";
    if (recheckFields().includes(event.target.name)) refreshSaveActions();
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
  if (event.target.closest("#settings-form") && ["device", "cpu_fallback"].includes(event.target.name)) {
    const form = $("settings-form");
    const precision = form.elements.compute_type;
    const cpu = form.elements.device.value === "cpu";
    for (const option of precision.options)
      option.disabled = cpu && option.value.includes("float16");
    if (cpu && precision.value.includes("float16")) {
      precision.value = "int8";
      $("precision-help").textContent = "Precision changed to INT8 because the CPU cannot use FP16. Save changes to apply.";
    }
    captureSettings();
    refreshSaveActions();
    const help = settings.device === "cuda" && !settings.capabilities?.cuda?.available
      ? settings.cpu_fallback
        ? "Crowbarr is set to use a graphics card, but none is available. Processing runs on the CPU instead."
        : "Crowbarr is set to use a graphics card, but none is available. Turn on CPU fallback or choose CPU, or jobs will not run."
      : "The CPU works on any machine. CUDA needs an NVIDIA graphics card that Crowbarr can reach.";
    $("device-help").textContent = help;
    dirty = draftDirty = true;
    $("save-state").textContent = "Unsaved changes";
  }
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
    const recheckChanged = recheckFields().some(
      (key) => savedSettings && settings[key] !== savedSettings[key],
    );
    const budgetChanged =
      savedSettings && settings.background_budget_minutes !== savedSettings.background_budget_minutes;
    const body =
      submit.dataset.keep === "true" ? { ...settings, carry_forward: true } : settings;
    settings = await api("/settings", "PUT", body);
    savedSettings = structuredClone(settings);
    dirty = draftDirty = false;
    toast(
      submit.dataset.keep === "true"
        ? "Settings saved. Finished results will be kept; pending and changed files use the new settings."
        : recheckChanged
          ? "Settings saved. Crowbarr will re-check the library with the new settings."
          : budgetChanged
            ? `Processing budget saved. Background work may run for ${settings.background_budget_minutes} minutes in each rolling hour.`
            : "Settings saved.",
    );
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
enter().catch((error) => {
  // Only an authentication failure means "sign in". Anything else is a fault worth
  // showing, not a login prompt that hides it.
  if (!session) return showLogin();
  $("notice").textContent = `Crowbarr could not start: ${error.message}`;
  $("notice").hidden = false;
  console.error(error);
});
// The server pushes state when it changes. The browser stops asking on a timer, so an
// idle library costs one heartbeat a minute instead of twenty requests.
let stream = null;
function listen() {
  if (stream) stream.close();
  stream = new EventSource("/api/events");
  stream.addEventListener("status", (event) => {
    try {
      apply(JSON.parse(event.data));
    } catch (error) {
      console.error(error);
    }
  });
  stream.addEventListener("open", () => {
    $("connection-status").textContent = "Live";
  });
  stream.addEventListener("error", () => {
    $("connection-status").textContent = "Reconnecting…";
    // EventSource retries on its own; only step in if the session ended.
  });
}
