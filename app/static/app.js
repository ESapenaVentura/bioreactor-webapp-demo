"use strict";

// Which sensors get their own chart. Everything gets a tile.
// Empty array = chart every sensor in the init payload.
const CHARTED = [];
const MAX_POINTS = 600;

// Mirrors app/assets/units.json — a static build asset, not sent on the wire.
const UNITS = {
  "Temperature": "°C",
  "pH": "pH",
  "DO": "%",
  "Agitation Speed": "rpm",
  "Air Flow Rate": "L/min",
  "OD600": "",
  "Glucose Concentration": "g/L",
  "Lactate Concentration": "g/L",
};

// The store keys sensors as "Reactor|Sensor". The live view shows ONE reactor at
// a time (tiles / charts, keyed by bare sensor name). The stream carries every
// reactor, though: history + anomaly detection run for all of them so an anomaly
// on a reactor you're not looking at still lands in the log.
let currentReactor = null;

const history = {};      // "Reactor|Sensor" -> {t:[], v:[], s:[]}   (all reactors)
const lastData = {};     // "Reactor|Sensor" -> last payload seen      (all reactors)

// Watchlists (server-authoritative). Kept per reactor so an off-screen reactor's
// breach can still show a warning by the dropdown.
const watchlistActive = {};   // reactor -> {sensor: {min?, max?}}
const watchlistNames = {};    // reactor -> [preset name]
let reactorWarnSig = "";      // last-rendered set of breaching reactors

const charts = {};       // sensor name -> uPlot          (current reactor only)
const chartWraps = {};   // sensor name -> HTMLElement     (current reactor only)
const tiles = {};        // sensor name -> HTMLElement     (current reactor only)

const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

const isCharted = (name) => CHARTED.length === 0 || CHARTED.includes(name);

function splitKey(key) {
  const i = key.indexOf("|");
  return i === -1 ? ["", key] : [key.slice(0, i), key.slice(i + 1)];
}
const keyFor = (name) => currentReactor + "|" + name;

// Local mirror of the server-side anomaly log (app/data/anomalies.json), oldest
// first. The server detects anomalies and ring-buffers the file at
// config.MAX_ANOMALIES *per reactor*; this array is just what the last fetch
// returned. Refreshed on a timer and after every acknowledge.
const anomalyLog = [];   // [{ id, reactor, sensor, value, at, kind, threshold, acked, ackedAt }]
let anomalyPage = 0;
const selected = new Set();

const PAGE_SIZE = 20;
const ANOMALY_POLL_MS = 3000;
const UI_KEY = "bioreactor.ui.v1";
const REACTOR_KEY = "bioreactor.reactor.v1";  // last selected reactor (global)

// View preferences. chartHidden is per reactor; collapsed sections + palette
// are global.
const chartHiddenByReactor = {};   // reactor -> Set<sensorName>
const collapsed = new Set();

function hiddenSet() {
  if (!currentReactor) return new Set();
  return chartHiddenByReactor[currentReactor] ||
    (chartHiddenByReactor[currentReactor] = new Set());
}

// What a tile latches onto for its sensor (by full "Reactor|Sensor" key).
// An unacknowledged watchlist breach (high/low) outranks a statistical anomaly —
// a hard limit crossing matters more than "unusual", so a later anomaly must not
// bury a still-open breach. Within a kind the most recent wins. With nothing
// open, fall back to the most recent row so the tile can render its cleared
// state.
function latchedIssue(key) {
  let breach, anomaly, newest;
  for (let i = anomalyLog.length - 1; i >= 0; i--) {
    const e = anomalyLog[i];
    if (e.reactor + "|" + e.sensor !== key) continue;
    if (!newest) newest = e;
    if (e.acked) continue;
    if (e.kind === "high" || e.kind === "low") { if (!breach) breach = e; }
    else if (!anomaly) anomaly = e;
  }
  return breach || anomaly || newest;
}

// ---------------------------------------------------------- anomaly log (server)

// Replace the local mirror with a fresh copy from the server and re-render
// everything that reads it. The server is the single source of truth.
function applyAnomalies(list) {
  anomalyLog.length = 0;
  if (Array.isArray(list)) {
    for (const e of list) anomalyLog.push(e);
  }
  for (const id of [...selected]) {
    if (!anomalyLog.some((e) => e.id === id)) selected.delete(id);
  }
  renderAnomalyTable();
  renderStatusBand();
  renderBreachIndicators();   // a latched breach clears from the strip on ack
  // Tiles latch onto their sensor's most recent open anomaly.
  for (const name of Object.keys(tiles)) {
    const d = lastData[keyFor(name)];
    if (d) renderTile(name, d);
    else renderAck(name);
  }
}

async function fetchAnomalies() {
  try {
    const d = await (await fetch("/api/anomalies")).json();
    applyAnomalies(d.anomalies);
  } catch (_) {
    /* transient — keep the last copy, the poll will retry */
  }
}

async function ackAnomalies(url, body) {
  try {
    const d = await (await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    })).json();
    applyAnomalies(d.anomalies);
  } catch (_) {
    fetchAnomalies();   // fall back to a plain refresh
  }
}

const acknowledge = (id) => ackAnomalies("/api/anomalies/ack", { ids: [id] });

// ---------------------------------------------------------------- localStorage

function saveUiPrefs() {
  try {
    const ch = {};
    for (const [r, set] of Object.entries(chartHiddenByReactor)) {
      if (set.size) ch[r] = [...set];
    }
    localStorage.setItem(UI_KEY, JSON.stringify({
      chartHiddenByReactor: ch,
      collapsed: [...collapsed],
      palette: document.documentElement.dataset.palette || "",
    }));
  } catch (_) { /* best effort */ }
}

function loadUiPrefs() {
  try {
    const p = JSON.parse(localStorage.getItem(UI_KEY) || "{}");
    for (const [r, arr] of Object.entries(p.chartHiddenByReactor || {})) {
      chartHiddenByReactor[r] = new Set(arr);
    }
    // migrate the old global list onto Cytiva Wave (the only reactor back then)
    if (Array.isArray(p.chartHidden) && p.chartHidden.length) {
      chartHiddenByReactor["Cytiva Wave"] = new Set(p.chartHidden);
    }
    for (const s of p.collapsed || []) collapsed.add(s);
    if (p.palette) document.documentElement.dataset.palette = p.palette;
  } catch (_) {
    localStorage.removeItem(UI_KEY);
  }
}

// ---------------------------------------------------------------- ingest

// Set when a tick shows a sensor entering an alarm state, so handleTick can pull
// the freshly-written server row now instead of waiting for the 3 s poll.
let anomalyLogStale = false;

// Runs for every sensor in every frame, regardless of which reactor is shown.
// Detection + logging now live on the server (see app/anomalies.py); the browser
// only keeps the per-sensor history for the charts and CSV export.
function ingest(key, reactor, name, d) {
  if (!history[key]) history[key] = { t: [], v: [], s: [] };
  const prev = lastData[key];
  if ((d.anomaly && !prev?.anomaly) ||
      (d.status !== "ok" && d.status !== (prev?.status ?? "ok"))) {
    anomalyLogStale = true;
  }
  lastData[key] = d;
  pushPoint(key, d.time, d.value, d.smoothed);
}

// ---------------------------------------------------------------- tiles

function ensureSensor(name) {
  if (!tiles[name]) tiles[name] = makeTile(name);
}

function makeTile(name) {
  const el = document.createElement("div");
  el.className = "tile";
  el.addEventListener("mouseenter", () => chartWraps[name]?.classList.add("highlight"));
  el.addEventListener("mouseleave", () => chartWraps[name]?.classList.remove("highlight"));
  el.dataset.sensor = name;
  const showGraph = !hiddenSet().has(name);
  el.innerHTML = `
    <div class="name"><span>${name}</span><span class="cat">${UNITS[name] || ""}</span></div>
    <div class="val">—</div>
    <div class="sub"><span class="roc"></span><span class="flags"></span></div>
    <div class="tile-tools">
      <label><input type="checkbox" class="chart-vis"${showGraph ? " checked" : ""}>Dashboard</label>
      <button type="button" class="show-graph">Show graph</button>
      <button type="button" class="tile-csv">Export CSV</button>
    </div>
    <div class="ack" hidden></div>`;
  document.getElementById("tiles").appendChild(el);
  return el;
}

// DOM update for a tile in the currently-shown reactor. No logging here.
function renderTile(name, d) {
  const el = tiles[name];
  if (!el) return;

  const open = latchedIssue(keyFor(name));
  const latched = !!(open && !open.acked);
  const targetId = latched ? open.id : "";
  if (el.dataset.ackId !== targetId) {
    el.dataset.ackId = targetId;
    renderAck(name);
  }

  const unit = UNITS[name] || "";
  el.querySelector(".val").innerHTML =
    `${d.value.toFixed(unit === "" ? 3 : 2)}<span class="u">${unit}</span>`;

  const sign = d.roc > 0 ? "+" : "";
  el.querySelector(".roc").textContent = `${sign}${d.roc.toFixed(3)}/min`;

  const flags = [];
  if (d.status !== "ok") flags.push(`<span class="badge ${d.status}">${d.status}</span>`);
  if (d.anomaly || latched) flags.push(`<span class="badge anom">anomaly</span>`);
  if (!d.status_ok) flags.push(`<span class="badge anom">bad quality</span>`);
  el.querySelector(".flags").innerHTML = flags.join(" ");

  el.className = "tile " + (d.anomaly || latched ? "anom" : d.status);
}

function renderAck(name) {
  const el = tiles[name];
  if (!el) return;
  const box = el.querySelector(".ack");
  if (!box) return;

  const open = latchedIssue(keyFor(name));
  if (!open || open.acked) {
    box.hidden = true;
    box.innerHTML = "";
    return;
  }

  const when = new Date(open.at * 1000).toLocaleTimeString();
  const what = open.kind === "high"
    ? `above limit${open.threshold != null ? ` (> ${open.threshold})` : ""}`
    : open.kind === "low"
      ? `below limit${open.threshold != null ? ` (< ${open.threshold})` : ""}`
      : "anomaly";
  box.hidden = false;
  box.innerHTML =
    `<span class="ack-when">⚠ ${what} at ${when}</span>` +
    `<button type="button" class="ack-btn" data-id="${open.id}">I acknowledge the anomaly</button>`;
}

// ---------------------------------------------------------------- anomaly table

const csvCell = (v) => {
  const s = v == null ? "" : String(v);
  return /[",\r\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
};

// Full ISO 8601 timestamp, made filename-safe: 2026-08-31T14-07-22.481Z
const stamp = () => new Date().toISOString().replace(/:/g, "-");
// Strip anything a filesystem might choke on out of a reactor / sensor name.
const safe = (s) => String(s).replace(/[^A-Za-z0-9_-]+/g, "_");

// rows = array of arrays; first row is the header. Triggers a file download.
function downloadCsv(filename, rows) {
  const csv = rows.map((r) => r.map(csvCell).join(",")).join("\r\n");
  const url = URL.createObjectURL(new Blob([csv], { type: "text/csv;charset=utf-8" }));
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function exportCsv(entries) {
  const head = ["reactor", "sensor", "type", "value", "threshold", "unit",
                "timestamp", "acknowledged", "acknowledged_at"];
  const rows = [head].concat(entries.map((e) => [
    e.reactor,
    e.sensor,
    (e.kind || "anomaly").toUpperCase(),
    e.value,
    e.threshold ?? "",
    UNITS[e.sensor] || "",
    new Date(e.at * 1000).toISOString(),
    e.acked ? "yes" : "no",
    e.ackedAt ? new Date(e.ackedAt * 1000).toISOString() : "",
  ]));
  downloadCsv(`${safe(currentReactor)}-anomalies-$stamp()}.csv`, rows);
}

// Full trend history for one sensor of the currently-shown reactor.
function exportSensorCsv(name) {
  const h = history[keyFor(name)];
  if (!h || !h.t.length) return;
  const unit = UNITS[name] ? ` (${UNITS[name]})` : "";
  const rows = [["timestamp", "value" + unit, "smoothed" + unit]];
  for (let i = 0; i < h.t.length; i++) {
    rows.push([new Date(h.t[i] * 1000).toISOString(), h.v[i], h.s[i] ?? ""]);
  }
  downloadCsv(`${safe(currentReactor)}-${safe(name)}-${stamp()}.csv`, rows);
}

function renderBulkBar() {
  const bar = document.getElementById("bulk-bar");
  bar.hidden = selected.size === 0;
  document.getElementById("bulk-count").textContent = `${selected.size} selected`;
}

function anomaliesInScope() {
  const all = document.getElementById("anom-all").checked;
  return anomalyLog.filter((e) => all || e.reactor === currentReactor);
}

function renderAnomalyTable() {
  const section = document.getElementById("anomalies");
  const tbody = document.getElementById("anomalies-rows");
  const table = document.querySelector(".anomalies table");
  const showAll = document.getElementById("anom-all").checked;

  // Drop selections for rows that fell out of the ring buffer.
  for (const id of selected) {
    if (!anomalyLog.some((e) => e.id === id)) selected.delete(id);
  }

  const scoped = anomaliesInScope();
  const rows = scoped.slice().reverse();                        // newest first
  const pageCount = Math.max(1, Math.ceil(rows.length / PAGE_SIZE));
  anomalyPage = Math.min(Math.max(0, anomalyPage), pageCount - 1);

  const unacked = scoped.reduce((n, e) => n + (e.acked ? 0 : 1), 0);
  document.getElementById("anomalies-count").textContent = scoped.length - unacked;
  const uBadge = document.getElementById("anomalies-unacked");
  uBadge.textContent = unacked;
  uBadge.hidden = unacked === 0;
  uBadge.title = `${unacked} unacknowledged`;

  const ackAll = document.getElementById("ack-all");
  if (ackAll) {
    ackAll.disabled = unacked === 0;
    ackAll.title = showAll
      ? `Acknowledge all ${unacked} across every reactor`
      : `Acknowledge all ${unacked} for ${currentReactor}`;
  }

  // Keep the section reachable while any anomaly exists (the toolbar lives in it).
  section.hidden = anomalyLog.length === 0;
  if (table) table.classList.toggle("all-reactors", showAll);

  const start = anomalyPage * PAGE_SIZE;
  const pageRows = rows.slice(start, start + PAGE_SIZE);

  tbody.innerHTML = pageRows.map((e) => {
    const unit = UNITS[e.sensor] || "";
    const value = e.value.toFixed(unit === "" ? 3 : 2) + (unit ? " " + unit : "");
    const when = new Date(e.at * 1000).toLocaleString();
    const kind = e.kind || "anomaly";
    const op = kind === "high" ? ">" : kind === "low" ? "<" : "";
    const thr = (kind !== "anomaly" && e.threshold != null)
      ? `<span class="kthr">${op} ${e.threshold}</span>` : "";
    const kindCell = `<span class="kbadge ${kind}">${kind}</span>${thr}`;
    const ackedTitle = e.ackedAt
      ? "acknowledged " + new Date(e.ackedAt * 1000).toLocaleString()
      : "acknowledged";
    const cell = e.acked
      ? `<span class="ack-yes" title="${ackedTitle}">✔</span>`
      : `<button type="button" class="ack-btn" data-id="${e.id}">I acknowledge the anomaly</button>`;
    return `<tr class="${e.acked ? "row-acked" : ""}">` +
      `<td class="sel-col"><input type="checkbox" class="row-sel" data-id="${e.id}"` +
        `${selected.has(e.id) ? " checked" : ""}></td>` +
      `<td class="reactor-col">${e.reactor}</td>` +
      `<td>${e.sensor}</td><td>${value}</td><td>${when}</td>` +
      `<td class="kind-col">${kindCell}</td>` +
      `<td class="ack-cell">${cell}</td></tr>`;
  }).join("");

  const selAll = document.querySelector(".sel-all");
  if (selAll) {
    const ids = pageRows.map((e) => e.id);
    selAll.checked = ids.length > 0 && ids.every((id) => selected.has(id));
    selAll.indeterminate = !selAll.checked && ids.some((id) => selected.has(id));
  }

  renderBulkBar();
  renderPager(pageCount);
}

function renderPager(pageCount) {
  const pager = document.getElementById("anomalies-pager");
  if (!pager) return;
  pager.hidden = pageCount <= 1;
  if (pager.hidden) { pager.innerHTML = ""; return; }
  pager.innerHTML =
    `<button type="button" data-page="-1"${anomalyPage === 0 ? " disabled" : ""}>‹ Prev</button>` +
    `<span>Page ${anomalyPage + 1} of ${pageCount}</span>` +
    `<button type="button" data-page="1"${anomalyPage >= pageCount - 1 ? " disabled" : ""}>Next ›</button>`;
}

// ---------------------------------------------------------------- charts

function chartOpts(width, height) {
  return {
    width: Math.max(160, width),
    height: Math.max(120, height),
    series: [
      {},
      { label: "raw", stroke: css("--faint"), width: 1, points: { show: false } },
      { label: "smoothed", stroke: css("--accent"), width: 2, points: { show: false } },
    ],
    axes: [
      {
        stroke: css("--faint"),
        grid: { stroke: css("--line") },
        ticks: { stroke: css("--line") },
        space: 70,
        values: "{HH}:{mm}:{ss}",
      },
      {
        stroke: css("--faint"),
        grid: { stroke: css("--line") },
        ticks: { stroke: css("--line") },
        size: 52,
      },
    ],
    legend: { live: true },
  };
}

function makeChart(name) {
  const wrap = document.createElement("div");
  wrap.className = "chart";
  wrap.dataset.sensor = name;
  wrap.innerHTML = `<h2>${name}${UNITS[name] ? " (" + UNITS[name] + ")" : ""}</h2>`;
  document.getElementById("charts").appendChild(wrap);
  chartWraps[name] = wrap;
  return new uPlot(chartOpts(wrap.clientWidth - 28, 200), [[], [], []], wrap);
}

function resizeAll() {
  for (const [name, chart] of Object.entries(charts)) {
    const wrap = chartWraps[name];
    if (wrap && !wrap.hidden && wrap.clientWidth) {
      chart.setSize({ width: wrap.clientWidth - 28, height: 200 });
    }
  }
  if (overlayChart) {
    const host = document.getElementById("overlay-chart");
    overlayChart.setSize({ width: host.clientWidth, height: Math.round(window.innerHeight * 0.55) });
  }
}
window.addEventListener("resize", resizeAll);

function applyChartVisibility() {
  const hidden = hiddenSet();
  for (const name of Object.keys(chartWraps)) {
    chartWraps[name].hidden = hidden.has(name);
  }
  const shown = Object.keys(chartWraps).filter((n) => !hidden.has(n)).length;
  document.getElementById("dashboard").dataset.empty = shown === 0 ? "1" : "0";
  document.getElementById("dashboard-count").textContent = shown;
}

function setChartVisible(name, visible) {
  const hidden = hiddenSet();
  if (visible) hidden.delete(name);
  else hidden.add(name);
  applyChartVisibility();
  if (visible) {
    const chart = charts[name], wrap = chartWraps[name];
    if (chart && wrap && wrap.clientWidth) {
      chart.setSize({ width: wrap.clientWidth - 28, height: 200 });
      redraw(name);
    }
  }
  const cb = tiles[name]?.querySelector(".chart-vis");
  if (cb) cb.checked = visible;
  saveUiPrefs();
}

// uPlot bakes colours in at construction — rebuild on a theme / palette flip.
function rebuildCharts() {
  for (const name of Object.keys(charts)) {
    charts[name].destroy();
    chartWraps[name].remove();
    delete charts[name];
    delete chartWraps[name];
    charts[name] = makeChart(name);
    redraw(name);
  }
  applyChartVisibility();
  resizeAll();
  if (overlaySensor) openOverlay(overlaySensor);
}
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", rebuildCharts);

function pushPoint(key, t, v, s) {
  const h = history[key];
  if (!h) return;
  if (h.t.length && t <= h.t[h.t.length - 1]) return;   // ignore replays
  h.t.push(t); h.v.push(v); h.s.push(s);
  if (h.t.length > MAX_POINTS) { h.t.shift(); h.v.shift(); h.s.shift(); }
}

function redraw(name) {
  const chart = charts[name], h = history[keyFor(name)];
  if (chart && h) chart.setData([h.t, h.v, h.s]);
  if (overlaySensor === name) redrawOverlay();
}

// ---------------------------------------------------------------- overlay

let overlayChart = null;
let overlaySensor = null;

function openOverlay(name) {
  if (overlayChart) { overlayChart.destroy(); overlayChart = null; }
  overlaySensor = name;

  document.getElementById("overlay-title").textContent =
    name + (UNITS[name] ? ` (${UNITS[name]})` : "");
  const host = document.getElementById("overlay-chart");
  host.innerHTML = "";
  document.getElementById("overlay").hidden = false;

  const h = history[keyFor(name)] || { t: [], v: [], s: [] };
  const w = host.clientWidth || 900;
  const ht = Math.round(window.innerHeight * 0.55);
  overlayChart = new uPlot(chartOpts(w, ht), [h.t.slice(), h.v.slice(), h.s.slice()], host);
}

function redrawOverlay() {
  const h = history[keyFor(overlaySensor)];
  if (overlayChart && h) overlayChart.setData([h.t, h.v, h.s]);
}

function closeOverlay() {
  if (overlayChart) { overlayChart.destroy(); overlayChart = null; }
  overlaySensor = null;
  const ov = document.getElementById("overlay");
  if (ov) ov.hidden = true;
}

// ---------------------------------------------------------------- reactor view

// Sync the live view (tiles, charts, sensor picker, title) to currentReactor,
// pulling from the history/lastData we keep for every reactor. Idempotent, so a
// plain reconnect just refreshes; a reactor switch swaps the sensor set.
function renderCurrentReactor() {
  if (!currentReactor) return;
  const prefix = currentReactor + "|";
  const keys = Object.keys(history).filter((k) => k.startsWith(prefix)).sort();
  const want = new Set(keys.map((k) => k.slice(prefix.length)));

  for (const name of Object.keys(tiles)) {
    if (!want.has(name)) { tiles[name].remove(); delete tiles[name]; }
  }
  for (const name of Object.keys(charts)) {
    if (!want.has(name)) {
      charts[name].destroy();
      chartWraps[name]?.remove();
      delete charts[name];
      delete chartWraps[name];
    }
  }

  document.querySelector("h1").textContent = currentReactor;
  const sensorSel = document.getElementById("sensor");
  sensorSel.innerHTML = "";

  for (const key of keys) {
    const name = key.slice(prefix.length);
    ensureSensor(name);
    const d = lastData[key];
    if (d) renderTile(name, d);
    renderAck(name);
    if (isCharted(name) && !charts[name]) charts[name] = makeChart(name);
    redraw(name);
    const o = document.createElement("option");
    o.value = o.textContent = name;
    sensorSel.appendChild(o);
  }

  applyChartVisibility();
  resizeAll();
  renderWatchlist();
}

async function loadWatchlistFor(reactor) {
  try {
    const d = await (await fetch("/api/watchlist?reactor=" + encodeURIComponent(reactor))).json();
    watchlistActive[reactor] = d.active || {};
    watchlistNames[reactor] = d.names || [];
  } catch (_) { /* ignore */ }
  if (reactor === currentReactor) { renderWatchlist(); renderBreachIndicators(); }
}

function switchReactor(name) {
  if (!name || name === currentReactor) return;
  currentReactor = name;
  try { localStorage.setItem(REACTOR_KEY, name); } catch (_) { /* ignore */ }
  selected.clear();
  closeOverlay();
  renderCurrentReactor();
  renderAnomalyTable();
  renderBreachIndicators();
  renderStatusBand();
  loadWatchlistFor(name);
  pollHealth();
}

let reactorList = [];

async function initReactors() {
  let data;
  try {
    data = await (await fetch("/api/reactors")).json();
  } catch (_) {
    data = { reactors: [], default: null };
  }
  reactorList = data.reactors || [];
  const sel = document.getElementById("reactor");
  sel.innerHTML = reactorList.map((n) => `<option value="${n}">${n}</option>`).join("");

  let saved = null;
  try { saved = localStorage.getItem(REACTOR_KEY); } catch (_) { /* ignore */ }
  currentReactor = (reactorList.includes(saved) && saved) || data.default || reactorList[0] || null;
  if (currentReactor) sel.value = currentReactor;
  document.querySelector("h1").textContent = currentReactor || "—";
  renderAnomalyTable();
}

// ---------------------------------------------------------------- watchlist

// Watchlist alarms on `reactor` that still need attention: either the value is
// currently outside its band (`live`), or it has recovered but the crossing is
// still unacknowledged in the log (latched). The top-of-page indicators — alarm
// strip, status-band chip, reactor-dropdown ⚠ — stay up until an operator
// acknowledges, the same way the tiles latch.
function reactorBreaches(reactor) {
  const prefix = reactor + "|";
  const byName = new Map();

  for (const [key, d] of Object.entries(lastData)) {
    if (!key.startsWith(prefix)) continue;
    if (d.status !== "low" && d.status !== "high") continue;
    const name = key.slice(prefix.length);
    const band = (watchlistActive[reactor] || {})[name] || {};
    byName.set(name, {
      sensor: name, status: d.status, value: d.value,
      threshold: d.status === "high" ? band.max : band.min,
      live: true,
    });
  }

  for (let i = anomalyLog.length - 1; i >= 0; i--) {
    const e = anomalyLog[i];
    if (e.reactor !== reactor || e.acked) continue;
    if (e.kind !== "high" && e.kind !== "low") continue;
    if (byName.has(e.sensor)) continue;          // a live entry already covers it
    byName.set(e.sensor, {
      sensor: e.sensor, status: e.kind, value: e.value,
      threshold: e.threshold, live: false,
    });
  }

  return [...byName.values()];
}

function fmtVal(name, v) {
  const u = UNITS[name] || "";
  return v.toFixed(u === "" ? 3 : 2) + (u ? " " + u : "");
}

// The sticky strip (current reactor) + the dropdown badge (other reactors).
function renderBreachIndicators() {
  const strip = document.getElementById("alarm-strip");
  const here = currentReactor ? reactorBreaches(currentReactor) : [];
  if (here.length) {
    strip.hidden = false;
    // amber, not red, once every alarm has recovered and only the ack is pending
    strip.classList.toggle("pending", here.every((b) => !b.live));
    strip.innerHTML =
      `<span>⚠ ${here.length} watchlist alarm${here.length > 1 ? "s" : ""}</span>` +
      `<span class="items">` + here.map((b) => {
        if (!b.live) return `${b.sensor} ${b.status.toUpperCase()} · awaiting acknowledgement`;
        const op = b.status === "high" ? ">" : "<";
        const t = b.threshold != null ? ` ${op} ${b.threshold}` : "";
        return `${b.sensor} ${b.status.toUpperCase()} ${fmtVal(b.sensor, b.value)}${t}`;
      }).join(" · ") + `</span>`;
  } else {
    strip.hidden = true;
    strip.classList.remove("pending");
    strip.innerHTML = "";
  }

  const others = reactorList.filter((r) => r !== currentReactor && reactorBreaches(r).length);
  const warn = document.getElementById("reactor-warn");
  warn.hidden = others.length === 0;
  warn.textContent = others.length ? `⚠ ${others.length}` : "";
  warn.dataset.jump = others[0] || "";
  warn.title = others.map((r) => {
    const bs = reactorBreaches(r).map((b) => `${b.sensor} ${b.status.toUpperCase()}`).join(", ");
    return `${r}: ${bs}`;
  }).join("  ·  ");

  // Prefix breaching reactors in the dropdown itself — only touch it on change.
  const breaching = new Set(reactorList.filter((r) => reactorBreaches(r).length));
  const sig = [...breaching].sort().join("|");
  if (sig !== reactorWarnSig) {
    reactorWarnSig = sig;
    for (const opt of document.getElementById("reactor").options) {
      opt.textContent = (breaching.has(opt.value) ? "⚠ " : "") + opt.value;
    }
  }
}

function watchlistSensors() {
  const prefix = currentReactor + "|";
  return Object.keys(history).filter((k) => k.startsWith(prefix))
    .map((k) => k.slice(prefix.length)).sort();
}

// Full rebuild — on reactor switch, preset load, add/remove. Not per tick (it
// would stomp on an input the user is editing).
function renderWatchlist() {
  const rows = document.getElementById("wl-rows");
  if (!currentReactor) { rows.innerHTML = ""; return; }

  const active = watchlistActive[currentReactor] || {};
  rows.innerHTML = Object.keys(active).sort().map((name) => {
    const band = active[name] || {};
    return `<div class="wl-row" data-sensor="${name}">
      <span class="wl-name">${name}</span>
      <input type="number" step="any" inputmode="decimal" class="wl-min" placeholder="min" value="${band.min ?? ""}">
      <input type="number" step="any" inputmode="decimal" class="wl-max" placeholder="max" value="${band.max ?? ""}">
      <span class="wl-unit">${UNITS[name] || ""}</span>
      <span class="wl-state"></span>
      <button type="button" class="wl-row-unload" data-sensor="${name}">Unload</button>
    </div>`;
  }).join("");

  // Builder dropdown: sensors on this reactor that aren't watched yet.
  const addSel = document.getElementById("wl-add-sensor");
  const free = watchlistSensors().filter((s) => !(s in active));
  addSel.innerHTML = free.length
    ? free.map((s) => `<option value="${s}">${s}${UNITS[s] ? " (" + UNITS[s] + ")" : ""}</option>`).join("")
    : `<option value="">all sensors watched</option>`;

  document.getElementById("wl-load").innerHTML = `<option value="">Load preset…</option>` +
    (watchlistNames[currentReactor] || []).map((n) => `<option>${n}</option>`).join("");

  clearBuilder();
  updateWatchlistStates();
}

function clearBuilder() {
  document.getElementById("wl-add-min").value = "";
  document.getElementById("wl-add-max").value = "";
  updateAddButton();
}

function parseInput(el) {
  return el.value === "" ? null : Number(el.value);
}

function updateAddButton() {
  const sensor = document.getElementById("wl-add-sensor").value;
  const lo = parseInput(document.getElementById("wl-add-min"));
  const hi = parseInput(document.getElementById("wl-add-max"));
  const ok = sensor && (lo != null || hi != null) &&
    !(lo != null && Number.isNaN(lo)) && !(hi != null && Number.isNaN(hi)) &&
    !(lo != null && hi != null && hi <= lo);
  document.getElementById("wl-add").disabled = !ok;
}

// Cheap per-tick refresh of just the breach badges + count.
function updateWatchlistStates() {
  const active = watchlistActive[currentReactor] || {};
  let breached = 0;
  for (const row of document.getElementById("wl-rows").querySelectorAll(".wl-row")) {
    const d = lastData[keyFor(row.dataset.sensor)] || {};
    const on = d.status === "low" || d.status === "high";
    if (on) breached++;
    row.classList.toggle("breached", on);
    row.querySelector(".wl-state").innerHTML =
      on ? `<span class="badge ${d.status}">${d.status}</span>` : "";
  }
  const set = Object.keys(active).length;
  document.getElementById("watchlist").dataset.empty = set === 0 ? "1" : "0";
  document.getElementById("wl-count").textContent = set;
  const wb = document.getElementById("wl-breached");
  wb.textContent = breached;
  wb.hidden = breached === 0;
  wb.title = `${breached} breached`;
}

// Read the watched rows' inline min/max inputs back into a thresholds dict.
function collectThresholds() {
  const th = {};
  for (const row of document.getElementById("wl-rows").querySelectorAll(".wl-row")) {
    const name = row.dataset.sensor;
    const min = parseInput(row.querySelector(".wl-min"));
    const max = parseInput(row.querySelector(".wl-max"));
    const bad = (min != null && Number.isNaN(min)) || (max != null && Number.isNaN(max)) ||
      (min != null && max != null && max <= min);
    row.classList.toggle("invalid", bad);
    if (bad || (min == null && max == null)) continue;
    th[name] = {};
    if (min != null) th[name].min = min;
    if (max != null) th[name].max = max;
  }
  return th;
}

async function postWatchlist(thresholds) {
  try {
    const d = await (await fetch("/api/watchlist", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ reactor: currentReactor, thresholds }),
    })).json();
    watchlistActive[currentReactor] = d.active || {};
  } catch (_) { /* keep local view; server will reconcile on next load */ }
}

async function loadAllWatchlists() {
  try {
    const all = await (await fetch("/api/watchlists")).json();
    for (const [r, d] of Object.entries(all)) {
      watchlistActive[r] = d.active || {};
      watchlistNames[r] = d.names || [];
    }
  } catch (_) { /* ignore */ }
  renderWatchlist();
  renderBreachIndicators();
}

// ------------------------------------------------------------------ socket

function setConnected(up, note) {
  const el = document.getElementById("sb-conn");
  const label = el.querySelector(".label");
  if (up) { el.className = "sb-conn live"; label.textContent = "live"; }
  else if (note === "reconnecting") { el.className = "sb-conn warn"; label.textContent = "reconnecting"; }
  else { el.className = "sb-conn warn"; label.textContent = "no instrument"; }
}

let lastTickAt = 0;

function relTime(ts) {
  const s = Math.max(0, Math.round((Date.now() - ts) / 1000));
  if (s < 3) return "just now";
  if (s < 60) return `${s}s ago`;
  return `${Math.round(s / 60)}m ago`;
}

// The always-on summary band: sensor count, watchlist breaches, unacknowledged
// anomalies (all scoped to the shown reactor), and data freshness.
function renderStatusBand() {
  // pollHealth owns the count until the stream is live and tiles exist.
  const n = Object.keys(tiles).length;
  if (n > 0) document.getElementById("sb-sensors").textContent = `${n} sensor${n === 1 ? "" : "s"}`;

  const breached = currentReactor ? reactorBreaches(currentReactor).length : 0;
  const b = document.getElementById("sb-breached");
  b.textContent = `${breached} breached`;
  b.classList.toggle("alert", breached > 0);

  const unacked = anomalyLog.reduce(
    (a, e) => a + (e.reactor === currentReactor && !e.acked ? 1 : 0), 0);
  const u = document.getElementById("sb-unacked");
  u.textContent = `${unacked} unacknowledged`;
  u.classList.toggle("alert", unacked > 0);

  document.getElementById("sb-updated").textContent =
    lastTickAt ? `updated ${relTime(lastTickAt)}` : "";
}

// init: { type, connected, sensors: { "Reactor|Sensor": {..full series..} } }
function handleInit(msg) {
  for (const [key, d] of Object.entries(msg.sensors)) {
    history[key] = {
      t: [...(d.times || [])],
      v: [...(d.values || [])],
      s: [...(d.smoothed_series || [])],
    };
    const [reactor, name] = splitKey(key);
    ingest(key, reactor, name, d);   // seed lastData + chart history
  }
  fetchAnomalies();                  // pull the shared log for the fresh tiles
  renderCurrentReactor();
  updateWatchlistStates();
  renderBreachIndicators();
  setConnected(msg.connected);
  renderStatusBand();
}

// tick: { type, connected, updates: { "Reactor|Sensor": {..latest..} } }
function handleTick(msg) {
  for (const [key, d] of Object.entries(msg.updates)) {
    const [reactor, name] = splitKey(key);
    ingest(key, reactor, name, d);
    if (reactor === currentReactor) {
      ensureSensor(name);
      renderTile(name, d);
      redraw(name);
    }
  }
  lastTickAt = Date.now();
  setConnected(msg.connected);
  updateWatchlistStates();
  renderBreachIndicators();
  renderStatusBand();
  if (anomalyLogStale) { anomalyLogStale = false; fetchAnomalies(); }
}

function connect() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const socket = new WebSocket(`${proto}//${location.host}/ws`);

  socket.onmessage = (event) => {
    const msg = JSON.parse(event.data);
    if (msg.type === "init") handleInit(msg);
    else if (msg.type === "tick") handleTick(msg);
  };

  socket.onclose = () => { setConnected(false, "reconnecting"); setTimeout(connect, 2000); };
  socket.onerror = () => socket.close();
}

// Authoritative per-reactor sensor count for the status band (tiles catch up).
async function pollHealth() {
  try {
    const q = currentReactor ? "?reactor=" + encodeURIComponent(currentReactor) : "";
    const h = await (await fetch("/api/health" + q)).json();
    const el = document.getElementById("sb-sensors");
    if (el) el.textContent = `${h.sensors} sensor${h.sensors === 1 ? "" : "s"}`;
  } catch (_) {
    /* keep the last value on a transient failure */
  }
}
setInterval(pollHealth, 3000);
setInterval(renderStatusBand, 1000);   // keeps "updated Ns ago" ticking
setInterval(fetchAnomalies, ANOMALY_POLL_MS);   // shared log, server-owned

// Restore persisted state before wiring the DOM.
fetchAnomalies();
loadUiPrefs();

// ------------------------------------------------------------------ events

document.getElementById("reactor").addEventListener("change", (e) => {
  switchReactor(e.target.value);
});

document.getElementById("anom-all").addEventListener("change", () => {
  selected.clear();
  anomalyPage = 0;
  renderAnomalyTable();
});

// --- watchlist controls ---

// Builder: enable "Add" only with a sensor + at least one valid bound.
for (const id of ["wl-add-sensor", "wl-add-min", "wl-add-max"]) {
  document.getElementById(id).addEventListener("input", updateAddButton);
  document.getElementById(id).addEventListener("change", updateAddButton);
}

document.getElementById("wl-add").addEventListener("click", async () => {
  const name = document.getElementById("wl-add-sensor").value;
  if (!name || document.getElementById("wl-add").disabled) return;
  const lo = parseInput(document.getElementById("wl-add-min"));
  const hi = parseInput(document.getElementById("wl-add-max"));
  const th = { ...(watchlistActive[currentReactor] || {}) };
  th[name] = {};
  if (lo != null) th[name].min = lo;
  if (hi != null) th[name].max = hi;
  await postWatchlist(th);
  renderWatchlist();
  renderBreachIndicators();
});

// Existing rows: inline edit of min/max, and per-sensor Unload.
document.getElementById("wl-rows").addEventListener("change", (e) => {
  if (e.target.matches(".wl-min, .wl-max")) postWatchlist(collectThresholds());
});

document.getElementById("wl-rows").addEventListener("click", async (e) => {
  const btn = e.target.closest(".wl-row-unload");
  if (!btn) return;
  const th = { ...(watchlistActive[currentReactor] || {}) };
  delete th[btn.dataset.sensor];
  await postWatchlist(th);
  renderWatchlist();
  renderBreachIndicators();
});

document.getElementById("wl-load").addEventListener("change", async (e) => {
  const name = e.target.value;
  e.target.value = "";                    // let the same preset be re-picked later
  if (!name) return;
  try {
    const url = `/api/watchlist/saved/${encodeURIComponent(name)}?reactor=${encodeURIComponent(currentReactor)}`;
    const d = await (await fetch(url)).json();
    await postWatchlist(d.thresholds || {});
    renderWatchlist();
  } catch (_) { /* ignore */ }
});

document.getElementById("wl-save").addEventListener("click", async () => {
  const name = (prompt(`Save this watchlist for ${currentReactor} as:`) || "").trim();
  if (!name) return;
  try {
    const d = await (await fetch(`/api/watchlist/saved/${encodeURIComponent(name)}`, {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ reactor: currentReactor, thresholds: collectThresholds() }),
    })).json();
    watchlistNames[currentReactor] = d.names || [];
    renderWatchlist();
  } catch (_) { /* ignore */ }
});

document.getElementById("wl-unload").addEventListener("click", async () => {
  try {
    await fetch("/api/watchlist?reactor=" + encodeURIComponent(currentReactor), { method: "DELETE" });
  } catch (_) { /* ignore */ }
  watchlistActive[currentReactor] = {};
  renderWatchlist();
  renderBreachIndicators();
});

// Jump to a reactor that has an off-screen watchlist alarm.
document.getElementById("reactor-warn").addEventListener("click", (e) => {
  const to = e.currentTarget.dataset.jump;
  if (to) { document.getElementById("reactor").value = to; switchReactor(to); }
});

document.getElementById("alarm-strip").addEventListener("click", () => {
  document.getElementById("watchlist").scrollIntoView({ behavior: "smooth", block: "start" });
});

document.getElementById("inject").addEventListener("click", async () => {
  const sensor = document.getElementById("sensor").value;
  if (!sensor || !currentReactor) return;
  await fetch("/api/debug/inject", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ reactor: currentReactor, sensor, sigmas: 10 }),
  });
});

// Tiles: acknowledge buttons + "Show graph" + "Export CSV".
document.getElementById("tiles").addEventListener("click", (e) => {
  const ack = e.target.closest(".ack-btn");
  if (ack) { acknowledge(ack.dataset.id); return; }
  const sg = e.target.closest(".show-graph");
  if (sg) { openOverlay(sg.closest(".tile").dataset.sensor); return; }
  const csv = e.target.closest(".tile-csv");
  if (csv) exportSensorCsv(csv.closest(".tile").dataset.sensor);
});

document.getElementById("tiles").addEventListener("change", (e) => {
  const cb = e.target.closest(".chart-vis");
  if (!cb) return;
  setChartVisible(cb.closest(".tile").dataset.sensor, cb.checked);
});

// Anomalies section: ack buttons + pager.
document.getElementById("anomalies").addEventListener("click", (e) => {
  const ack = e.target.closest(".ack-btn");
  if (ack) { acknowledge(ack.dataset.id); return; }
  const nav = e.target.closest("[data-page]");
  if (nav && !nav.disabled) { anomalyPage += Number(nav.dataset.page); renderAnomalyTable(); }
});

// Row + select-all checkboxes.
document.getElementById("anomalies").addEventListener("change", (e) => {
  const row = e.target.closest(".row-sel");
  if (row) {
    if (e.target.checked) selected.add(row.dataset.id);
    else selected.delete(row.dataset.id);
    renderAnomalyTable();
    return;
  }
  if (e.target.closest(".sel-all")) {
    const start = anomalyPage * PAGE_SIZE;
    const pageIds = anomaliesInScope().slice().reverse()
      .slice(start, start + PAGE_SIZE).map((x) => x.id);
    if (e.target.checked) pageIds.forEach((id) => selected.add(id));
    else pageIds.forEach((id) => selected.delete(id));
    renderAnomalyTable();
  }
});

document.getElementById("bulk-csv").addEventListener("click", () => {
  const chosen = anomalyLog.filter((x) => selected.has(x.id));
  if (chosen.length) exportCsv(chosen);
});

document.getElementById("bulk-ack").addEventListener("click", () => {
  const ids = [...selected];
  selected.clear();
  if (ids.length) ackAnomalies("/api/anomalies/ack", { ids });
});

document.getElementById("bulk-clear").addEventListener("click", () => {
  selected.clear();
  renderAnomalyTable();
});

// "Acknowledge all" in the table header — every unacknowledged row currently in
// scope (this reactor, or all reactors when that filter is on), across all pages.
document.getElementById("ack-all").addEventListener("click", () => {
  const showAll = document.getElementById("anom-all").checked;
  ackAnomalies("/api/anomalies/ack-all", { reactor: showAll ? null : currentReactor });
});

// Collapsible sections (Dashboard, Anomalies).
for (const toggle of document.querySelectorAll(".section-toggle")) {
  const id = toggle.getAttribute("aria-controls");
  const panel = document.getElementById(id);
  const sectionId = toggle.closest(".section").id;

  if (collapsed.has(sectionId)) {
    panel.hidden = true;
    toggle.setAttribute("aria-expanded", "false");
  }

  toggle.addEventListener("click", () => {
    const opening = panel.hidden;
    panel.hidden = !opening;
    toggle.setAttribute("aria-expanded", String(opening));
    if (opening) collapsed.delete(sectionId);
    else collapsed.add(sectionId);
    if (opening && panel.contains(document.getElementById("charts"))) resizeAll();
    saveUiPrefs();
  });
}

// Colour-blind palette toggle.
const cvdToggle = document.getElementById("cvd-toggle");
cvdToggle.checked = document.documentElement.dataset.palette === "cvd";
cvdToggle.addEventListener("change", () => {
  if (cvdToggle.checked) document.documentElement.dataset.palette = "cvd";
  else document.documentElement.removeAttribute("data-palette");
  saveUiPrefs();
  rebuildCharts();
});

// Overlay dismissal.
document.getElementById("overlay-close").addEventListener("click", closeOverlay);
document.getElementById("overlay").addEventListener("click", (e) => {
  if (e.target.id === "overlay") closeOverlay();
});
window.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !document.getElementById("overlay").hidden) closeOverlay();
});

renderAnomalyTable();
initReactors().then(() => { connect(); pollHealth(); loadAllWatchlists(); });
