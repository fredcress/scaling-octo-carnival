"use strict";

/* ================================================================ helpers */

const $ = (sel, root = document) => root.querySelector(sel);
const view = $("#view");
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

async function api(path, opts = {}) {
  const res = await fetch(path, {
    credentials: "same-origin",
    ...opts,
    headers: { "X-Requested-With": "fetch", ...(opts.headers || {}) },
  });
  if (res.status === 401) { location.href = "/login"; throw new Error("login required"); }
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.detail || res.statusText);
  return body;
}

function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(toast._h);
  toast._h = setTimeout(() => t.classList.remove("show"), 3200);
}

const fmt = {
  km: (m, d = 1) => m == null ? "–" : (m / 1000).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d }),
  dur(s) {
    if (s == null) return "–";
    s = Math.round(s);
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
    return h ? `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}` : `${m}:${String(sec).padStart(2, "0")}`;
  },
  hours(s) {
    if (!s) return "0h";
    const h = Math.floor(s / 3600), m = Math.round((s % 3600) / 60);
    return h ? `${h}h ${String(m).padStart(2, "0")}m` : `${m}m`;
  },
  pace(sPerKm) {
    if (!sPerKm || !isFinite(sPerKm)) return "–";
    const m = Math.floor(sPerKm / 60), s = Math.round(sPerKm % 60);
    return s === 60 ? `${m + 1}:00` : `${m}:${String(s).padStart(2, "0")}`;
  },
  speed: (mps) => mps ? (mps * 3.6).toFixed(1) : "–",
  int: (x) => x == null ? "–" : Math.round(x).toLocaleString(),
  date: (iso, opts = { day: "numeric", month: "short", year: "numeric" }) =>
    iso ? new Date(iso.length === 10 ? iso + "T12:00:00" : iso).toLocaleDateString(undefined, opts) : "–",
  dateTime: (iso) => iso ? new Date(iso).toLocaleString(undefined,
    { weekday: "short", day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" }) : "–",
  weekLabel: (iso) => fmt.date(iso, { day: "numeric", month: "short" }),
};

/* paced activities show min/km, the rest km/h */
const paced = (a) => ["running", "walking", "hiking"].includes(a.sport);
const paceOrSpeed = (a) => paced(a)
  ? `${fmt.pace(a.distance_m ? a.moving_s / a.distance_m * 1000 : null)}<small>/km</small>`
  : `${fmt.speed(a.avg_speed)}<small>km/h</small>`;

/* ================================================================ icons */

const I = (d, extra = "") => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" ${extra}>${d}</svg>`;
const icons = {
  dash: I('<rect x="3" y="3" width="7" height="9" rx="1.5"/><rect x="14" y="3" width="7" height="5" rx="1.5"/><rect x="14" y="12" width="7" height="9" rx="1.5"/><rect x="3" y="16" width="7" height="5" rx="1.5"/>'),
  list: I('<path d="M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01"/>'),
  trend: I('<path d="M3 17l6-6 4 4 8-8"/><path d="M14 7h7v7"/>'),
  trophy: I('<path d="M8 21h8M12 17v4M7 4h10v5a5 5 0 0 1-10 0V4zM17 5h3v2a3 3 0 0 1-3 3M7 5H4v2a3 3 0 0 0 3 3"/>'),
  map: I('<path d="M9 4L3 6v14l6-2 6 2 6-2V4l-6 2-6-2z"/><path d="M9 4v14M15 6v14"/>'),
  gear: I('<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/>'),
  logout: I('<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9"/>'),
  run: I('<circle cx="14" cy="4" r="2"/><path d="M7 21l3-6 3 2v5M6 12l3-4 4 1 3 4h3M10 15l-1-4"/>'),
  ride: I('<circle cx="6" cy="17" r="3.5"/><circle cx="18" cy="17" r="3.5"/><path d="M6 17l4-8h5l3 8M10 9l3 8M13 5h3"/>'),
  walk: I('<circle cx="12" cy="4" r="2"/><path d="M10 21l2-7 3 3v4M9 9l3-2 3 4 3 1M12 7l-2 7"/>'),
  other: I('<circle cx="12" cy="12" r="8"/><path d="M12 8v4l3 2"/>'),
  check: I('<path d="M20 6L9 17l-5-5"/>'),
  alert: I('<circle cx="12" cy="12" r="9"/><path d="M12 8v5M12 16h.01"/>'),
  ext: I('<path d="M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/>'),
  upload: I('<path d="M12 16V4M6 10l6-6 6 6M4 20h16"/>'),
  star: I('<path d="M12 3l2.7 5.6 6.1.9-4.4 4.3 1 6.1L12 17l-5.4 2.9 1-6.1-4.4-4.3 6.1-.9z" fill="currentColor"/>'),
  refresh: I('<path d="M21 12a9 9 0 1 1-2.6-6.4L21 8M21 3v5h-5"/>'),
  flag: I('<path d="M5 21V4M5 4h11l-2 4 2 4H5"/>'),
};
const sportIcon = (sport) =>
  `<span class="sport-ico">${{ running: icons.run, cycling: icons.ride, walking: icons.walk, hiking: icons.walk }[sport] || icons.other}</span>`;

/* ================================================================ nav + router */

const NAV = [
  ["#/", "Dashboard", icons.dash],
  ["#/plan", "Plan", icons.flag],
  ["#/activities", "Activities", icons.list],
  ["#/trends", "Trends", icons.trend],
  ["#/records", "Records", icons.trophy],
  ["#/map", "Heatmap", icons.map],
  ["#/settings", "Settings", icons.gear],
];
$("#nav").insertAdjacentHTML("beforeend",
  NAV.map(([h, label, ic]) => `<a href="${h}" data-route="${h}">${ic}<span>${label}</span></a>`).join("") +
  `<div class="spacer"></div><a class="logout" href="/logout">${icons.logout}<span>Sign out</span></a>`);

let charts = [];
let maps = [];
let timers = [];
function cleanup() {
  charts.forEach((c) => c.destroy());
  maps.forEach((m) => m.remove());
  timers.forEach(clearInterval);
  charts = []; maps = []; timers = [];
}

const routes = [
  [/^#\/?$/, renderDashboard],
  [/^#\/plan$/, renderPlan],
  [/^#\/activities$/, renderActivities],
  [/^#\/activity\/(\d+)$/, renderActivity],
  [/^#\/trends$/, renderTrends],
  [/^#\/records$/, renderRecords],
  [/^#\/map$/, renderHeatmap],
  [/^#\/settings$/, renderSettings],
];

async function route() {
  cleanup();
  const hash = location.hash.split("?")[0] || "#/";
  document.querySelectorAll(".nav a[data-route]").forEach((a) => {
    const r = a.dataset.route;
    a.classList.toggle("active", r === "#/" ? hash === "#/" || hash === "#" || hash === ""
      : hash.startsWith(r) || (r === "#/activities" && hash.startsWith("#/activity/")));
  });
  for (const [re, fn] of routes) {
    const m = hash.match(re);
    if (m) {
      try { await fn(...m.slice(1)); }
      catch (e) { if (e.message !== "login required") view.innerHTML = `<div class="notice err">${icons.alert}${esc(e.message)}</div>`; }
      return;
    }
  }
  location.hash = "#/";
}
window.addEventListener("hashchange", () => { window.scrollTo(0, 0); route(); });
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", route);

/* ================================================================ charts */

function chartTheme() {
  return {
    ink: css("--ink"), ink2: css("--ink-2"), muted: css("--muted"), grid: css("--grid"),
    axis: css("--axis"), surface: css("--surface"), s1: css("--series-1"), s2: css("--series-2"),
    s3: css("--series-3"), neg: css("--diverge-neg"),
    zones: [1, 2, 3, 4, 5].map((i) => css(`--z${i}`)),
  };
}

function alpha(hex, a) {
  const n = parseInt(hex.slice(1), 16);
  return `rgba(${n >> 16},${(n >> 8) & 255},${n & 255},${a})`;
}

function baseOptions(t, { yTitle, yFmt, xFmt, reverseY = false, stacked = false, xLinear = false, legend = false } = {}) {
  Chart.defaults.font.family = css("--font");
  Chart.defaults.font.size = 12;
  Chart.defaults.color = t.muted;
  return {
    responsive: true,
    maintainAspectRatio: false,
    animation: false,
    interaction: { mode: "index", intersect: false },
    plugins: {
      legend: { display: legend },
      tooltip: {
        backgroundColor: t.surface, titleColor: t.ink, bodyColor: t.ink2,
        borderColor: t.axis, borderWidth: 1, padding: 10, cornerRadius: 8,
        boxPadding: 4, usePointStyle: true,
        titleFont: { weight: "600" },
      },
    },
    scales: {
      x: {
        type: xLinear ? "linear" : "category",
        stacked,
        grid: { display: false },
        border: { color: t.axis },
        ticks: { maxRotation: 0, autoSkipPadding: 14, callback: xFmt ? function (v) { return xFmt(xLinear ? v : this.getLabelForValue(v)); } : undefined },
      },
      y: {
        stacked,
        reverse: reverseY,
        grid: { color: t.grid, lineWidth: 1 },
        border: { display: false },
        title: yTitle ? { display: true, text: yTitle, color: t.muted } : undefined,
        ticks: { callback: yFmt, maxTicksLimit: 6 },
      },
    },
  };
}

const barStyle = (color) => ({
  backgroundColor: color,
  hoverBackgroundColor: color,
  maxBarThickness: 24,
  borderRadius: { topLeft: 4, topRight: 4, bottomLeft: 0, bottomRight: 0 },
  borderSkipped: "start",
});

const lineStyle = (color, fill = false) => ({
  borderColor: color,
  backgroundColor: fill ? alpha(color, 0.1) : color,
  borderWidth: 2,
  pointRadius: 0,
  pointHoverRadius: 5,
  pointHoverBorderWidth: 2,
  pointHoverBorderColor: css("--surface"),
  tension: 0.25,
  fill,
  borderJoinStyle: "round",
  borderCapStyle: "round",
  spanGaps: true,
});

function makeChart(canvasId, config) {
  const c = new Chart($(`#${canvasId}`), config);
  charts.push(c);
  return c;
}

function rolling(values, n) {
  const out = [];
  for (let i = 0; i < values.length; i++) {
    const win = values.slice(Math.max(0, i - n + 1), i + 1).filter((v) => v != null);
    out.push(win.length ? win.reduce((a, b) => a + b, 0) / win.length : null);
  }
  return out;
}

/* ================================================================ strava button */

function stravaCell(a, { big = false } = {}) {
  const cls = big ? "" : "small";
  switch (a.strava_status) {
    case "done":
      return `<a class="pill good" href="https://www.strava.com/activities/${a.strava_activity_id}" target="_blank" rel="noopener">${icons.check} On Strava</a>`;
    case "duplicate":
      return `<a class="pill" href="https://www.strava.com/activities/${a.strava_activity_id}" target="_blank" rel="noopener" title="Strava already had this activity">${icons.check} Already on Strava</a>`;
    case "uploading":
      return `<span class="pill" data-strava-wait="${a.id}">${icons.upload} Uploading…</span>`;
    case "error":
      return `<button class="strava ${cls}" data-strava="${a.id}" title="${esc(a.strava_error)}">${icons.refresh} Retry Strava</button>`;
    default:
      return `<button class="strava ${cls}" data-strava="${a.id}">${icons.upload} Push to Strava</button>`;
  }
}

/* one delegated handler for every Push to Strava button on any page */
document.addEventListener("click", async (ev) => {
  const btn = ev.target.closest("[data-strava]");
  if (!btn) return;
  ev.preventDefault();
  ev.stopPropagation();
  const id = btn.dataset.strava;
  btn.disabled = true;
  try {
    await api(`/api/activities/${id}/strava`, { method: "POST" });
    btn.outerHTML = stravaCell({ id, strava_status: "uploading" }, { big: !btn.classList.contains("small") });
    watchUploads();
  } catch (e) {
    btn.disabled = false;
    toast(e.message);
  }
});

function watchUploads() {
  if (watchUploads.timer) return;
  watchUploads.timer = setInterval(async () => {
    const pending = [...document.querySelectorAll("[data-strava-wait]")];
    if (!pending.length) { clearInterval(watchUploads.timer); watchUploads.timer = null; return; }
    for (const el of pending) {
      const id = el.dataset.stravaWait;
      try {
        const s = await api(`/api/activities/${id}/strava`);
        if (s.strava_status !== "uploading") {
          el.outerHTML = stravaCell({ id, ...s });
          if (s.strava_status === "error") toast(`Strava: ${s.strava_error}`);
          else toast("Uploaded to Strava");
        }
      } catch { /* keep polling */ }
    }
  }, 2500);
}

/* ================================================================ shared bits */

function activityRows(list) {
  return list.map((a) => `
    <tr class="link" data-href="#/activity/${a.id}">
      <td>${fmt.date(a.start_local, { weekday: "short", day: "numeric", month: "short", year: "numeric" })}</td>
      <td>${sportIcon(a.sport)}<span class="act-name">${esc(a.name)}</span></td>
      <td class="r">${fmt.km(a.distance_m, 2)} km</td>
      <td class="r">${fmt.dur(a.moving_s)}</td>
      <td class="r">${paceOrSpeed(a).replace(/<\/?small>/g, " ")}</td>
      <td class="r">${fmt.int(a.avg_hr)}</td>
      <td class="r" title="${a.trimp_estimated ? "Estimated (no heart rate)" : "TRIMP"}">${fmt.int(a.trimp)}${a.trimp_estimated ? "*" : ""}</td>
      <td>${stravaCell(a)}</td>
    </tr>`).join("");
}

const activityTable = (list) => `
  <div class="table-wrap"><table>
    <thead><tr><th>Date</th><th>Activity</th><th class="r">Distance</th><th class="r">Time</th>
      <th class="r">Pace / speed</th><th class="r">Avg HR</th><th class="r">Load</th><th>Strava</th></tr></thead>
    <tbody>${activityRows(list)}</tbody>
  </table></div>`;

document.addEventListener("click", (ev) => {
  if (ev.target.closest("a, button")) return;
  const tr = ev.target.closest("tr[data-href]");
  if (tr) location.hash = tr.dataset.href;
});

function delta(cur, prev, { lowerIsBetter = false, unit = "", fmtFn = (x) => x.toFixed(1) } = {}) {
  if (cur == null || prev == null || !prev) return `<div class="delta">no comparison yet</div>`;
  const diff = cur - prev;
  if (Math.abs(diff) < 1e-9) return `<div class="delta">same as before</div>`;
  const good = lowerIsBetter ? diff < 0 : diff > 0;
  const arrow = diff > 0 ? "▲" : "▼";
  return `<div class="delta ${good ? "good" : "bad"}">${arrow} ${fmtFn(Math.abs(diff))}${unit}</div>`;
}

function formState(tsb) {
  if (tsb == null) return "";
  if (tsb > 5) return `<span class="pill good">${icons.check} Fresh</span>`;
  if (tsb >= -10) return `<span class="pill">Neutral</span>`;
  if (tsb >= -30) return `<span class="pill good">${icons.trend} Building</span>`;
  return `<span class="pill err">${icons.alert} High fatigue</span>`;
}

function emptyState() {
  return `<div class="card empty">
    <h2>No runs yet</h2>
    <p>Drop .fit files into the watched folder (Syncthing from Gadgetbridge).<br>
    New files are picked up automatically, or trigger a scan from <a href="#/settings">Settings</a>.</p>
  </div>`;
}

/* ================================================================ dashboard */

async function renderDashboard() {
  const s = await api("/api/summary");
  if (!s.total_activities) { view.innerHTML = `<div class="page-head"><h1>Dashboard</h1></div>${emptyState()}`; return; }
  const w = s.this_week, lw = s.last_week, cur = s.current || {};
  view.innerHTML = `
    <div class="page-head">
      <div><h1>Dashboard</h1><div class="sub">${fmt.date(s.today, { weekday: "long", day: "numeric", month: "long" })}</div></div>
      ${s.unsent ? `<a class="btn" href="#/activities">${icons.upload} ${s.unsent} not on Strava yet</a>` : ""}
    </div>

    <div class="card">
      <div class="hero">
        <div>
          <div class="sub">This week</div>
          <div class="hero-value">${fmt.km(w.distance_m)}<small>km</small></div>
          ${delta(w.distance_m / 1000, s.last_week_to_date.distance_m / 1000, { unit: " km vs this point last week" })}
          <div class="delta">Last week: ${fmt.km(lw.distance_m)} km in ${lw.runs} runs</div>
        </div>
        <div class="side">
          <div class="stat"><div class="label">Runs</div><div class="value">${w.runs}</div></div>
          <div class="stat"><div class="label">Time</div><div class="value">${fmt.hours(w.moving_s)}</div></div>
          <div class="stat"><div class="label">Avg pace</div><div class="value">${fmt.pace(w.pace_s_per_km)} /km</div></div>
          <div class="stat"><div class="label">${new Date(s.today).toLocaleDateString(undefined, { month: "long" })}</div><div class="value">${fmt.km(s.this_month.distance_m, 0)} km</div><div class="delta">${s.this_month.runs} runs · ${fmt.hours(s.this_month.moving_s)}</div></div>
          <div class="stat"><div class="label">${s.today.slice(0, 4)} so far</div><div class="value">${fmt.km(s.this_year.distance_m, 0)} km</div><div class="delta">${s.this_year.runs} runs · ${fmt.hours(s.this_year.moving_s)}</div></div>
        </div>
      </div>
    </div>

    <div class="tiles">
      <div class="card tile"><div class="label">Fitness</div><div class="value">${fmt.int(cur.ctl)}</div><div class="delta">42-day load average</div></div>
      <div class="card tile"><div class="label">Fatigue</div><div class="value">${fmt.int(cur.atl)}</div><div class="delta">7-day load average</div></div>
      <div class="card tile"><div class="label">Form</div><div class="value">${cur.tsb != null ? (cur.tsb > 0 ? "+" : "") + Math.round(cur.tsb) : "–"}</div><div class="delta">${formState(cur.tsb)}</div></div>
      <div class="card tile"><div class="label">Pace, last 28 days</div><div class="value">${fmt.pace(s.pace_28d)}<small>/km</small></div>
        ${delta(s.pace_28d, s.pace_prev_28d, { lowerIsBetter: true, unit: " vs prior 28 d", fmtFn: fmt.pace })}</div>
      <div class="card tile"><div class="label">VO₂max (watch)</div><div class="value">${s.vo2max ? s.vo2max.value : "–"}</div><div class="delta">${s.vo2max ? "as of " + fmt.date(s.vo2max.date) : "not in FIT files yet"}</div></div>
    </div>

    <div class="grid cols-2">
      <div class="card">
        <div class="card-head"><h2>Weekly distance</h2><span class="sub">last 26 weeks, km</span></div>
        <div class="chart"><canvas id="c-weeks"></canvas></div>
      </div>
      <div class="card">
        <div class="card-head"><h2>Fitness &amp; fatigue</h2>
          <div class="legend"><span><i class="key line" style="background:var(--series-1)"></i>Fitness</span><span><i class="key line" style="background:var(--series-2)"></i>Fatigue</span></div>
        </div>
        <div class="chart"><canvas id="c-fitness"></canvas></div>
      </div>
    </div>
    <div class="card">
      <div class="card-head"><h2>Form</h2><span class="sub">fitness − fatigue · positive = fresh, negative = training hard</span></div>
      <div class="chart sm"><canvas id="c-form"></canvas></div>
    </div>

    <div class="card">
      <div class="card-head"><h2>Recent activities</h2><a href="#/activities">All activities</a></div>
      ${activityTable(s.recent)}
    </div>`;

  const t = chartTheme();
  makeChart("c-weeks", {
    type: "bar",
    data: {
      labels: s.weeks.map((x) => x.week),
      datasets: [{ label: "Distance", data: s.weeks.map((x) => +(x.distance_m / 1000).toFixed(1)), ...barStyle(t.s1) }],
    },
    options: (() => {
      const o = baseOptions(t, { xFmt: fmt.weekLabel });
      o.interaction = { mode: "index", intersect: false };
      o.plugins.tooltip.callbacks = {
        title: (it) => `Week of ${fmt.weekLabel(s.weeks[it[0].dataIndex].week)}`,
        label: (it) => {
          const wk = s.weeks[it.dataIndex];
          return [` ${fmt.km(wk.distance_m)} km · ${wk.runs} runs`, ` ${fmt.hours(wk.moving_s)} · longest ${fmt.km(wk.long_run_m)} km`];
        },
      };
      return o;
    })(),
  });

  const fit = s.fitness;
  makeChart("c-fitness", {
    type: "line",
    data: {
      labels: fit.map((x) => x.date),
      datasets: [
        { label: "Fitness", data: fit.map((x) => x.ctl), ...lineStyle(t.s1) },
        { label: "Fatigue", data: fit.map((x) => x.atl), ...lineStyle(t.s2) },
      ],
    },
    options: baseOptions(t, { xFmt: fmt.weekLabel }),
  });
  makeChart("c-form", {
    type: "bar",
    data: {
      labels: fit.map((x) => x.date),
      datasets: [{
        label: "Form", data: fit.map((x) => x.tsb),
        backgroundColor: fit.map((x) => x.tsb >= 0 ? t.s1 : t.neg),
        barPercentage: 1, categoryPercentage: 0.9, borderRadius: 2, borderSkipped: false,
      }],
    },
    options: baseOptions(t, { xFmt: fmt.weekLabel }),
  });
  watchUploads();
}

/* ================================================================ training plan */

const RACES = [[5000, "5 km"], [10000, "10 km"], [21097.5, "Half marathon"], [42195, "Marathon"]];
const WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
const PHASES = {
  base: ["Base", "Easy volume and strides to build the aerobic engine"],
  build: ["Build", "Threshold and interval sessions, volume still rising"],
  peak: ["Peak", "Race-pace work and your longest runs"],
  taper: ["Taper", "Less volume, a little race pace: arrive fresh"],
  race: ["Race week", "Short and easy, then race"],
};
const SESSION_COLOR = { easy: "--z1", long: "--z2", fartlek: "--z3", threshold: "--z3", intervals: "--z4", race_pace: "--z5", race: "--strava" };

function parseDuration(text) {
  const parts = String(text).trim().split(":").map(Number);
  if (!text.trim() || parts.some((p) => !isFinite(p) || p < 0) || parts.length < 2 || parts.length > 3) return null;
  return parts.reduce((acc, p) => acc * 60 + p, 0);
}

function sessionStatus(s) {
  return {
    done: `<span class="pill good">${icons.check} Done</span>`,
    partial: `<span class="pill">Partly done</span>`,
    missed: `<span class="pill err">Missed</span>`,
    today: `<span class="pill today">Today</span>`,
  }[s.status] || "";
}

function sessionRow(s, today) {
  const done = s.runs.map((r) => `<a href="#/activity/${r.id}">${fmt.km(r.distance_km * 1000, 1)} km</a>`).join(", ");
  return `<tr class="${s.date === today ? "is-today" : ""}">
    <td class="muted">${fmt.date(s.date, { weekday: "short", day: "numeric", month: "short" })}</td>
    <td class="wrap"><i class="key" style="background:var(${SESSION_COLOR[s.type] || "--z1"})"></i>
      <span class="act-name">${esc(s.title)}</span><div class="sub">${esc(s.detail)}</div></td>
    <td class="r">${fmt.km(s.distance_km * 1000, 1)} km</td>
    <td class="r">${done ? done : ""}</td>
    <td>${sessionStatus(s)}</td>
  </tr>`;
}

function weekTable(w, today) {
  const extra = w.extra_runs.map((r) => `<tr><td class="muted">${fmt.date(r.date, { weekday: "short", day: "numeric", month: "short" })}</td>
    <td class="wrap muted">Extra run · ${esc(r.name)}</td><td></td><td class="r"><a href="#/activity/${r.id}">${fmt.km(r.distance_km * 1000, 1)} km</a></td><td></td></tr>`).join("");
  return `<div class="table-wrap"><table class="plan-table">
    <thead><tr><th>Day</th><th>Session</th><th class="r">Planned</th><th class="r">Done</th><th></th></tr></thead>
    <tbody>${w.sessions.map((s) => sessionRow(s, today)).join("")}${extra}</tbody></table></div>`;
}

async function renderPlan() {
  const d = await api("/api/plan");
  const editing = new URLSearchParams(location.hash.split("?")[1] || "").has("edit");
  if (!d.plan || editing) return renderPlanForm(d);
  const p = d.plan, c = p.config, today = p.today;
  // set up late in the week, the plan starts next Monday: preview its first week
  const upcoming = !p.current_week && !p.finished;
  const cur = p.weeks.find((w) => w.index === p.current_week) || (upcoming ? p.weeks[0] : null);
  const wksLeft = Math.ceil(p.days_to_race / 7);
  const goal = c.goal_time_s;

  let verdict = "";
  if (goal && p.vdot_current) {
    const gap = p.vdot_goal - p.vdot_current, weeks = p.weeks.length;
    verdict = gap <= 0 ? ["ok", `Your current fitness already predicts ${fmt.dur(p.predicted_time_s)}. The goal is within reach; you could aim higher.`]
      : gap <= weeks * 0.25 ? ["ok", `Realistic: the goal needs about ${gap.toFixed(1)} VDOT points more than you show today, over ${weeks} weeks.`]
      : gap <= weeks * 0.4 ? ["warn", `Ambitious: ${gap.toFixed(1)} VDOT points to gain in ${weeks} weeks. Possible if training goes well.`]
      : ["err", `Very ambitious: ${gap.toFixed(1)} VDOT points in ${weeks} weeks is a big jump. Around ${fmt.dur(p.predicted_time_s)} is what you're running now.`];
  } else if (!p.vdot_current) {
    verdict = ["warn", goal ? "No recent 5 km+ efforts found, so all paces come from your goal time."
      : "No recent 5 km+ efforts and no goal time, so paces are a cautious guess. Add a goal time or rebuild after a few runs."];
  }

  const pc = p.paces;
  const paceRows = [
    ["Easy &amp; long runs", `${fmt.pace(pc.easy[0])}–${fmt.pace(pc.easy[1])}`, "Most of your running. You should be able to talk."],
    c.distance_m === 42195 ? ["Marathon", fmt.pace(pc.marathon), "Steady and sustainable."] : null,
    ["Threshold", fmt.pace(pc.threshold), "Comfortably hard: about the pace you could hold for an hour."],
    ["Interval", fmt.pace(pc.interval), "Hard, 3–5 min repeats. Builds VO₂max."],
    ["Race pace", fmt.pace(pc.race), goal ? `Your goal: ${fmt.dur(goal)}` : `Predicted: ${fmt.dur(pc.race * c.distance_m / 1000)}`],
  ].filter(Boolean);

  const thisWeek = cur ? (() => {
    const ws = new Date(cur.start + "T12:00:00");
    const rows = WEEKDAYS.map((_, i) => {
      const day = new Date(ws.getTime() + i * 86400000).toISOString().slice(0, 10);
      const s = cur.sessions.find((x) => x.date === day);
      if (s) return sessionRow(s, today);
      const extra = cur.extra_runs.filter((r) => r.date === day);
      return `<tr class="${day === today ? "is-today" : ""}"><td class="muted">${fmt.date(day, { weekday: "short", day: "numeric", month: "short" })}</td>
        <td class="muted">${extra.length ? `Extra run · ${esc(extra[0].name)}` : "Rest"}</td><td></td>
        <td class="r">${extra.map((r) => `<a href="#/activity/${r.id}">${fmt.km(r.distance_km * 1000, 1)} km</a>`).join(", ")}</td><td></td></tr>`;
    }).join("");
    return `<div class="card">
      <div class="card-head"><h2>${upcoming ? `First week, starting ${fmt.date(cur.start, { weekday: "long", day: "numeric", month: "short" })}` : "This week"} · ${PHASES[cur.phase][0]}${cur.recovery ? " (easier week)" : ""}</h2>
        <span class="sub">${fmt.km(cur.actual_km * 1000)} of ${fmt.km(cur.planned_km * 1000)} km</span></div>
      <p class="sub" style="margin-top:-4px">${PHASES[cur.phase][1]}${cur.recovery ? ". Lighter on purpose so your body absorbs the last three weeks." : "."}</p>
      <div class="table-wrap"><table class="plan-table">
        <thead><tr><th>Day</th><th>Session</th><th class="r">Planned</th><th class="r">Done</th><th></th></tr></thead>
        <tbody>${rows}</tbody></table></div>
    </div>`;
  })() : "";

  view.innerHTML = `
    <div class="page-head">
      <div><h1>${esc(c.race_name || p.label)}</h1>
        <div class="sub">${esc(p.label)} · ${fmt.date(c.race_date, { weekday: "long", day: "numeric", month: "long", year: "numeric" })}</div></div>
      <div style="display:flex;gap:8px"><a class="btn" href="#/plan?edit=1">Edit / rebuild</a><button id="plan-del">Delete</button></div>
    </div>
    ${p.finished ? `<div class="notice ok">${icons.check}Race day has passed. Hope it went well! <a href="#/plan?edit=1" style="margin-left:6px">Set up the next one</a></div>` : ""}
    ${verdict ? `<div class="notice ${verdict[0]}">${verdict[0] === "ok" ? icons.check : icons.alert}${verdict[1]}</div>` : ""}
    <div class="tiles">
      <div class="card tile"><div class="label">Race in</div><div class="value">${p.finished ? "–" : p.days_to_race}<small>days</small></div>
        <div class="delta">${p.finished ? "done" : `${wksLeft} week${wksLeft === 1 ? "" : "s"}`}</div></div>
      <div class="card tile"><div class="label">Goal</div><div class="value">${goal ? fmt.dur(goal) : "–"}</div>
        <div class="delta">${goal ? fmt.pace(goal / c.distance_m * 1000) + " /km" : "no goal time set"}</div></div>
      <div class="card tile"><div class="label">Predicted today</div><div class="value">${p.predicted_time_s ? fmt.dur(p.predicted_time_s) : "–"}</div>
        <div class="delta">${p.vdot_current ? `VDOT ${p.vdot_current} at plan start` : "no recent efforts"}</div></div>
      <div class="card tile"><div class="label">Phase</div><div class="value">${cur ? PHASES[cur.phase][0] : "–"}</div>
        <div class="delta">${!cur ? "" : upcoming ? `starts ${fmt.weekLabel(cur.start)}` : `week ${cur.index} of ${p.weeks.length}`}</div></div>
    </div>
    ${thisWeek}
    <div class="grid cols-2">
      <div class="card">
        <div class="card-head"><h2>Weekly distance</h2>
          <div class="legend"><span><i class="key planned"></i>Planned</span><span><i class="key" style="background:var(--series-1)"></i>Done</span></div></div>
        <div class="chart"><canvas id="c-plan"></canvas></div>
      </div>
      <div class="card">
        <div class="card-head"><h2>Your paces</h2><span class="sub">min/km</span></div>
        <dl class="kv paces">${paceRows.map(([k, v, note]) => `<dt>${k}</dt><dd><strong class="num">${v}</strong> <span class="sub">${note}</span></dd>`).join("")}</dl>
        <p class="sub">Set from your fitness when the plan was built. Rebuild every 4–6 weeks so paces keep up as you get fitter.</p>
      </div>
    </div>
    <div class="card">
      <div class="card-head"><h2>Full plan</h2><span class="sub">${p.weeks.length} weeks · ${fmt.km(p.weeks.reduce((a, w) => a + w.planned_km, 0) * 1000, 0)} km</span></div>
      ${p.weeks.map((w) => `<details class="plan-week" ${w.index === p.current_week ? "open" : ""}>
        <summary><span class="wk">Week ${w.index}</span><span class="phase ph-${w.phase}">${PHASES[w.phase][0]}${w.recovery ? " · easier" : ""}</span>
          <span class="muted">${fmt.weekLabel(w.start)}</span><span class="spacer"></span>
          <span class="num">${w.start <= today ? `${fmt.km(w.actual_km * 1000)} / ` : ""}${fmt.km(w.planned_km * 1000)} km</span></summary>
        ${weekTable(w, today)}
      </details>`).join("")}
    </div>`;

  $("#plan-del").onclick = async () => {
    if (!confirm("Delete this training plan? Your runs are not affected.")) return;
    await api("/api/plan", { method: "DELETE" });
    route();
  };

  const t = chartTheme();
  const o = baseOptions(t, { xFmt: fmt.weekLabel });
  o.scales.x.stacked = false;
  o.plugins.tooltip.callbacks = {
    title: (it) => { const w = p.weeks[it[0].dataIndex]; return `Week ${w.index} · ${PHASES[w.phase][0]} · ${fmt.weekLabel(w.start)}`; },
    label: (it) => ` ${it.dataset.label}: ${fmt.km(it.parsed.y * 1000)} km`,
  };
  makeChart("c-plan", {
    type: "bar",
    data: {
      labels: p.weeks.map((w) => w.start),
      datasets: [
        { label: "Planned", data: p.weeks.map((w) => w.planned_km), ...barStyle(alpha(t.muted, 0.3)), grouped: false, order: 2 },
        { label: "Done", data: p.weeks.map((w) => w.start <= today ? w.actual_km : null), ...barStyle(t.s1), grouped: false, order: 1, barPercentage: 0.55 },
      ],
    },
    options: o,
  });
}

function renderPlanForm(d) {
  const p = d.plan, c = p ? p.config : null, b = d.baseline;
  const minDate = new Date(Date.now() + 7 * 86400000).toISOString().slice(0, 10);
  const dist = c ? c.distance_m : 21097.5;
  view.innerHTML = `
    <div class="page-head"><div><h1>${p ? "Rebuild training plan" : "Training plan"}</h1>
      <div class="sub">Pick a race and the app builds a week-by-week plan from your recent running.</div></div></div>
    <div class="grid cols-2">
      <form class="card form" id="plan-form">
        <label>Race name <span class="muted">(optional)</span><input name="race_name" maxlength="80" placeholder="e.g. Semi de Paris" value="${esc(c ? c.race_name : "")}"></label>
        <label>Distance<select name="distance_m">${RACES.map(([m, l]) => `<option value="${m}" ${m === dist ? "selected" : ""}>${l}</option>`).join("")}</select></label>
        <label>Race date<input type="date" name="race_date" required min="${minDate}" value="${c && c.race_date >= minDate ? c.race_date : ""}"></label>
        <label>Goal time <span class="muted">(optional, h:mm:ss)</span><input name="goal" placeholder="1:45:00" inputmode="numeric" value="${c && c.goal_time_s ? fmt.dur(c.goal_time_s) : ""}">
          <span class="sub" id="goal-hint"></span></label>
        <div class="row">
          <label>Runs per week<select name="runs_per_week">${[3, 4, 5, 6].map((n) => `<option ${n === (c ? c.runs_per_week : 4) ? "selected" : ""}>${n}</option>`).join("")}</select></label>
          <label>Long run on<select name="long_run_day">${WEEKDAYS.map((w, i) => `<option value="${i}" ${i === (c ? c.long_run_day : 6) ? "selected" : ""}>${w}</option>`).join("")}</select></label>
        </div>
        <div style="display:flex;gap:8px;margin-top:6px">
          <button class="primary" type="submit">${p ? "Rebuild plan" : "Build plan"}</button>
          ${p ? `<a class="btn" href="#/plan">Cancel</a>` : ""}
        </div>
        ${p ? `<p class="sub">Rebuilding starts the plan again from this week, using your current fitness.</p>` : ""}
      </form>
      <div class="card">
        <div class="card-head"><h2>Your starting point</h2><span class="sub">from your runs</span></div>
        <dl class="kv">
          <dt>Weekly distance</dt><dd>${fmt.km(b.week_km * 1000)} km <span class="sub">average, last 4 weeks</span></dd>
          <dt>Longest run</dt><dd>${fmt.km(b.long_km * 1000)} km <span class="sub">last 6 weeks</span></dd>
          <dt>Fitness</dt><dd>${b.vdot ? `VDOT ${b.vdot} <span class="sub">from a ${esc(b.vdot_from.label)} in ${fmt.dur(b.vdot_from.time_s)}</span>` : `<span class="sub">no 5 km+ effort in the last 90 days</span>`}</dd>
          <dt>Predicted now</dt><dd id="pred">–</dd>
        </dl>
        <p class="sub">Predictions use your fastest stretches inside training runs, so they're on the cautious side if you've mostly run easy.</p>
        <p class="sub">The plan raises weekly distance by at most about 10%, makes every fourth week lighter, and tapers before race day.</p>
      </div>
    </div>`;

  const form = $("#plan-form");
  const update = () => {
    const pred = b.predictions[String(+form.distance_m.value)];
    $("#pred").innerHTML = pred ? `${fmt.dur(pred)} <span class="sub">${fmt.pace(pred / form.distance_m.value * 1000)} /km</span>` : "–";
    const g = parseDuration(form.goal.value);
    $("#goal-hint").textContent = g ? `${fmt.pace(g / form.distance_m.value * 1000)} /km` : "";
  };
  form.distance_m.onchange = update;
  form.goal.oninput = update;
  update();
  form.onsubmit = async (e) => {
    e.preventDefault();
    const goal = form.goal.value.trim() ? parseDuration(form.goal.value) : null;
    if (form.goal.value.trim() && !goal) { toast("Goal time should look like 1:45:00 or 48:30"); return; }
    const btn = form.querySelector("button[type=submit]");
    btn.disabled = true;
    try {
      await api("/api/plan", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          race_name: form.race_name.value.trim(), race_date: form.race_date.value,
          distance_m: +form.distance_m.value, goal_time_s: goal,
          runs_per_week: +form.runs_per_week.value, long_run_day: +form.long_run_day.value,
        }),
      });
      if (location.hash === "#/plan") route(); else location.hash = "#/plan";
    } catch (err) { toast(err.message); btn.disabled = false; }
  };
}

/* ================================================================ activities */

async function renderActivities() {
  const kind = sessionStorageGet("kind") || "run";
  const list = await api(`/api/activities?kind=${kind}`);
  view.innerHTML = `
    <div class="page-head">
      <div><h1>Activities</h1><div class="sub">${list.length} ${kind === "run" ? "runs" : "activities"}</div></div>
      <div class="seg" id="kind">
        <button data-k="run" class="${kind === "run" ? "on" : ""}">Runs</button>
        <button data-k="all" class="${kind === "all" ? "on" : ""}">All activities</button>
      </div>
    </div>
    ${list.length ? `<div class="card">${activityTable(list)}</div>` : emptyState()}`;
  $("#kind").addEventListener("click", (e) => {
    const k = e.target.dataset.k;
    if (k) { sessionStorageSet("kind", k); route(); }
  });
  watchUploads();
}

function sessionStorageGet(k) { try { return sessionStorage.getItem(k); } catch { return null; } }
function sessionStorageSet(k, v) { try { sessionStorage.setItem(k, v); } catch { /* ignore */ } }

/* ================================================================ activity detail */

async function renderActivity(id) {
  const a = await api(`/api/activities/${id}`);
  const st = a.streams || {};
  const isPaced = paced(a);
  const tile = (label, value) => `<div class="card tile"><div class="label">${label}</div><div class="value">${value}</div></div>`;
  const tiles = [
    tile("Distance", `${fmt.km(a.distance_m, 2)}<small>km</small>`),
    tile("Moving time", fmt.dur(a.moving_s)),
    tile(isPaced ? "Avg pace" : "Avg speed", paceOrSpeed(a)),
    a.avg_hr ? tile("Avg heart rate", `${fmt.int(a.avg_hr)}<small>bpm</small>`) : "",
    a.max_hr ? tile("Max heart rate", `${fmt.int(a.max_hr)}<small>bpm</small>`) : "",
    a.ascent_m != null ? tile("Elevation gain", `${fmt.int(a.ascent_m)}<small>m</small>`) : "",
    a.avg_cadence ? tile("Cadence", `${fmt.int(a.avg_cadence)}<small>${a.sport === "running" ? "spm" : "rpm"}</small>`) : "",
    a.avg_power ? tile("Avg power", `${fmt.int(a.avg_power)}<small>W</small>`) : "",
    tile("Load", `${fmt.int(a.trimp)}${a.trimp_estimated ? "<small>est.</small>" : ""}`),
    a.training_effect ? tile("Training effect", `${a.training_effect.toFixed(1)}<small>aerobic</small>`) : "",
    a.anaerobic_te ? tile("Anaerobic TE", `${a.anaerobic_te.toFixed(1)}`) : "",
    a.calories ? tile("Calories", fmt.int(a.calories)) : "",
  ].join("");

  const hasDist = st.dist && st.dist.some((v) => v);
  const charts_ = [];
  if (st.speed) charts_.push(["c-pace", isPaced ? "Pace (min/km)" : "Speed (km/h)"]);
  if (st.hr) charts_.push(["c-hr", "Heart rate (bpm)"]);
  if (st.alt) charts_.push(["c-alt", "Elevation (m)"]);
  if (st.cad) charts_.push(["c-cad", `Cadence (${a.sport === "running" ? "spm" : "rpm"})`]);
  if (st.power) charts_.push(["c-pow", "Power (W)"]);

  const zoneTotal = (a.hr_zones || []).reduce((x, y) => x + y, 0);
  const zl = a.zone_limits;
  const zoneHtml = a.hr_zones ? a.hr_zones.map((sec, i) => `
    <div class="zone-row">
      <span>Z${i + 1} <span class="muted num">${zl[i]}${i < 4 ? "–" + (zl[i + 1] - 1) : "+"}</span></span>
      <div class="zone-track"><div class="zone-fill" style="width:${zoneTotal ? sec / zoneTotal * 100 : 0}%;background:var(--z${i + 1})"></div></div>
      <span class="r num">${fmt.dur(sec)}</span>
    </div>`).join("") : "";

  const splits = a.splits || [];
  const laps = (a.laps || []).length > 1 ? a.laps : [];
  const effortRows = (a.best_efforts || []).map((e) => `
    <tr><td>${esc(e.label)}</td><td class="r">${fmt.dur(e.time_s)}</td><td class="r">${fmt.pace(e.time_s / e.distance_m * 1000)} /km</td>
    <td>${e.is_pr ? `<span class="pill pr">${icons.star} PR</span>` : ""}</td></tr>`).join("");

  view.innerHTML = `
    <div class="page-head">
      <div>
        <div class="sub"><a href="#/activities">← Activities</a></div>
        <h1>${sportIcon(a.sport)}${esc(a.name)}</h1>
        <div class="sub">${fmt.dateTime(a.start_local)}${a.device ? " · " + esc(a.device) : ""}</div>
      </div>
      <div>${stravaCell(a, { big: true })}</div>
    </div>
    <div class="tiles">${tiles}</div>
    ${a.polyline ? `<div class="card"><div class="map" id="map"></div></div>` : ""}
    <div class="grid cols-2">
      ${charts_.map(([cid, title]) => `<div class="card"><div class="card-head"><h2>${title}</h2></div><div class="chart sm"><canvas id="${cid}"></canvas></div></div>`).join("")}
    </div>
    <div class="grid cols-2">
      ${a.hr_zones ? `<div class="card"><div class="card-head"><h2>Time in heart rate zones</h2></div><div class="zone-bars">${zoneHtml}</div></div>` : ""}
      ${effortRows ? `<div class="card"><div class="card-head"><h2>Best efforts</h2></div><div class="table-wrap"><table><tbody>${effortRows}</tbody></table></div></div>` : ""}
    </div>
    ${splits.length ? `<div class="card"><div class="card-head"><h2>Splits</h2><span class="sub">per km</span></div><div class="table-wrap"><table>
      <thead><tr><th>Km</th><th class="r">Distance</th><th class="r">Time</th><th class="r">Pace</th><th class="r">Avg HR</th><th class="r">Gain</th></tr></thead>
      <tbody>${splits.map((s) => `<tr><td>${s.km}</td><td class="r">${fmt.km(s.distance_m, 2)}</td><td class="r">${fmt.dur(s.time_s)}</td>
        <td class="r">${fmt.pace(s.pace_s_per_km)}</td><td class="r">${fmt.int(s.avg_hr)}</td><td class="r">${fmt.int(s.gain_m)} m</td></tr>`).join("")}</tbody>
    </table></div></div>` : ""}
    ${laps.length ? `<div class="card"><div class="card-head"><h2>Laps</h2></div><div class="table-wrap"><table>
      <thead><tr><th>Lap</th><th class="r">Distance</th><th class="r">Time</th><th class="r">Pace</th><th class="r">Avg HR</th><th class="r">Max HR</th></tr></thead>
      <tbody>${laps.map((l) => `<tr><td>${l.lap}</td><td class="r">${fmt.km(l.distance_m, 2)}</td><td class="r">${fmt.dur(l.time_s)}</td>
        <td class="r">${fmt.pace(l.pace_s_per_km)}</td><td class="r">${fmt.int(l.avg_hr)}</td><td class="r">${fmt.int(l.max_hr)}</td></tr>`).join("")}</tbody>
    </table></div></div>` : ""}`;

  if (a.polyline) {
    const m = L.map("map", { scrollWheelZoom: false });
    maps.push(m);
    L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19, attribution: "&copy; OpenStreetMap contributors",
    }).addTo(m);
    const line = L.polyline(a.polyline, { color: css("--series-2"), weight: 3.5, opacity: 0.95 }).addTo(m);
    L.circleMarker(a.polyline[0], { radius: 6, color: css("--surface"), weight: 2, fillColor: css("--good"), fillOpacity: 1 }).addTo(m);
    L.circleMarker(a.polyline.at(-1), { radius: 6, color: css("--surface"), weight: 2, fillColor: css("--critical"), fillOpacity: 1 }).addTo(m);
    m.fitBounds(line.getBounds(), { padding: [20, 20] });
  }

  // x axis: distance (km) when available, otherwise elapsed minutes
  const t = chartTheme();
  const xs = hasDist ? st.dist.map((d) => d == null ? null : d / 1000) : st.t.map((s) => s / 60);
  const xFmt = (v) => hasDist ? `${(+v).toFixed(1)} km` : `${Math.round(v)} min`;
  const series = (vals) => xs.map((x, i) => ({ x, y: vals[i] })).filter((p) => p.x != null);
  const mk = (cid, vals, color, opts = {}) => {
    const o = baseOptions(t, { xLinear: true, xFmt, ...opts });
    o.scales.x.min = xs.find((v) => v != null);
    o.scales.x.max = xs.at(-1);
    o.plugins.tooltip.callbacks = {
      title: (it) => xFmt(it[0].parsed.x),
      label: (it) => ` ${opts.tipFmt ? opts.tipFmt(it.parsed.y) : Math.round(it.parsed.y)}`,
    };
    makeChart(cid, { type: "line", data: { datasets: [{ data: series(vals), ...lineStyle(color, opts.fill), tension: 0 }] }, options: o });
  };
  if (st.speed) {
    const smooth = rolling(st.speed, 8);
    if (isPaced) {
      const pace = smooth.map((v) => v && v > 0.5 ? 1000 / v : null);
      const sorted = pace.filter(Boolean).sort((x, y) => x - y);
      const hi = sorted[Math.floor(sorted.length * 0.98)] || 600;
      mk("c-pace", pace.map((p) => p && p <= hi * 1.15 ? p : null), t.s1, { reverseY: true, yFmt: fmt.pace, tipFmt: (v) => fmt.pace(v) + " /km" });
    } else {
      mk("c-pace", smooth.map((v) => v == null ? null : v * 3.6), t.s1, { tipFmt: (v) => v.toFixed(1) + " km/h" });
    }
  }
  if (st.hr) mk("c-hr", st.hr, t.s2, { tipFmt: (v) => Math.round(v) + " bpm" });
  if (st.alt) mk("c-alt", st.alt, t.s3, { fill: true, tipFmt: (v) => Math.round(v) + " m" });
  if (st.cad) mk("c-cad", rolling(st.cad, 5), t.s1, { tipFmt: (v) => Math.round(v) });
  if (st.power) mk("c-pow", rolling(st.power, 5), t.s2, { tipFmt: (v) => Math.round(v) + " W" });
  watchUploads();
}

/* ================================================================ trends */

async function renderTrends() {
  const d = await api("/api/trends");
  if (!d.runs.length) { view.innerHTML = `<div class="page-head"><h1>Trends</h1></div>${emptyState()}`; return; }
  const hasVo2 = d.runs.some((r) => r.vo2max);
  const hasCad = d.runs.some((r) => r.cadence);
  const zl = d.zone_limits;
  view.innerHTML = `
    <div class="page-head"><div><h1>Trends</h1><div class="sub">Runs only · each dot is one run, the line is the average of the last 10</div></div></div>
    <div class="card">
      <div class="card-head"><h2>Monthly distance</h2><span class="sub">km</span></div>
      <div class="chart"><canvas id="c-month"></canvas></div>
    </div>
    <div class="grid cols-2">
      <div class="card">
        <div class="card-head"><h2>Average pace</h2>${trendLegend()}</div>
        <div class="chart"><canvas id="c-pace"></canvas></div>
      </div>
      <div class="card">
        <div class="card-head"><h2>Aerobic efficiency</h2>${trendLegend()}</div>
        <div class="sub" style="margin:-6px 0 8px">metres per heartbeat · higher = more speed for the same effort</div>
        <div class="chart"><canvas id="c-eff"></canvas></div>
      </div>
    </div>
    <div class="card">
      <div class="card-head"><h2>Weekly time in heart rate zones</h2><span class="sub">hours</span></div>
      <div class="legend" style="margin-bottom:10px">${[1, 2, 3, 4, 5].map((i) =>
        `<span><i class="key" style="background:var(--z${i})"></i>Z${i} ${zl[i - 1]}${i < 5 ? "–" + (zl[i] - 1) : "+"}</span>`).join("")}</div>
      <div class="chart"><canvas id="c-zones"></canvas></div>
    </div>
    <div class="grid cols-2">
      ${hasCad ? `<div class="card"><div class="card-head"><h2>Cadence</h2>${trendLegend()}</div><div class="chart"><canvas id="c-cad"></canvas></div></div>` : ""}
      ${hasVo2 ? `<div class="card"><div class="card-head"><h2>VO₂max (watch estimate)</h2></div><div class="chart"><canvas id="c-vo2"></canvas></div></div>` : ""}
    </div>`;

  // skip leading empty months, but always show at least 6
  const firstMonth = d.monthly.findIndex((m) => m.runs > 0);
  d.monthly = d.monthly.slice(Math.min(Math.max(firstMonth, 0), d.monthly.length - 6));

  const t = chartTheme();
  makeChart("c-month", {
    type: "bar",
    data: { labels: d.monthly.map((m) => m.month), datasets: [{ label: "Distance", data: d.monthly.map((m) => +(m.distance_m / 1000).toFixed(1)), ...barStyle(t.s1) }] },
    options: (() => {
      const o = baseOptions(t, { xFmt: (m) => new Date(m + "-15").toLocaleDateString(undefined, { month: "short", year: "2-digit" }) });
      o.plugins.tooltip.callbacks = {
        title: (it) => new Date(d.monthly[it[0].dataIndex].month + "-15").toLocaleDateString(undefined, { month: "long", year: "numeric" }),
        label: (it) => { const m = d.monthly[it.dataIndex]; return ` ${fmt.km(m.distance_m)} km · ${m.runs} runs · ${fmt.hours(m.moving_s)}`; },
      };
      return o;
    })(),
  });

  const runs = d.runs;
  const ts = runs.map((r) => new Date(r.date + "T12:00:00").getTime());
  const dateFmt = (v) => new Date(v).toLocaleDateString(undefined, { month: "short", year: "2-digit" });
  const scatterTrend = (cid, key, color, opts = {}) => {
    const vals = runs.map((r) => r[key]);
    const roll = rolling(vals, 10);
    const o = baseOptions(t, { xLinear: true, xFmt: dateFmt, ...opts });
    o.interaction = { mode: "nearest", intersect: false, axis: "x" };
    o.scales.x.min = ts[0];
    o.scales.x.max = ts.at(-1);
    o.plugins.tooltip.callbacks = {
      title: (it) => { const r = runs[it[0].dataIndex]; return `${fmt.date(r.date)} · ${esc(r.name)}`; },
      label: (it) => ` ${it.dataset.label}: ${opts.tipFmt ? opts.tipFmt(it.parsed.y) : it.parsed.y.toFixed(2)}`,
    };
    const c = makeChart(cid, {
      type: "scatter",
      data: {
        datasets: [
          { label: "Run", data: vals.map((v, i) => ({ x: ts[i], y: v })), pointRadius: 3.5, pointHoverRadius: 6,
            backgroundColor: alpha(t.muted, 0.55), borderColor: t.surface, borderWidth: 1 },
          { label: "10-run avg", type: "line", data: roll.map((v, i) => ({ x: ts[i], y: v })), ...lineStyle(color) },
        ],
      },
      options: o,
    });
    c.canvas.style.cursor = "pointer";
    c.canvas.onclick = (ev) => {
      const pts = c.getElementsAtEventForMode(ev, "nearest", { intersect: true }, false);
      if (pts.length) location.hash = `#/activity/${runs[pts[0].index].id}`;
    };
  };
  scatterTrend("c-pace", "pace_s_per_km", t.s1, { reverseY: true, yFmt: fmt.pace, tipFmt: (v) => fmt.pace(v) + " /km" });
  scatterTrend("c-eff", "efficiency", t.s1, { yFmt: (v) => v.toFixed(2), tipFmt: (v) => v.toFixed(2) + " m/beat" });
  if (hasCad) scatterTrend("c-cad", "cadence", t.s1, { tipFmt: (v) => Math.round(v) + " spm" });
  if (hasVo2) {
    const v = runs.filter((r) => r.vo2max);
    const o = baseOptions(t, { xLinear: true, xFmt: dateFmt });
    o.plugins.tooltip.callbacks = { title: (it) => fmt.date(v[it[0].dataIndex].date), label: (it) => ` VO₂max ${it.parsed.y}` };
    makeChart("c-vo2", { type: "line", data: { datasets: [{ label: "VO₂max", data: v.map((r) => ({ x: new Date(r.date + "T12:00:00").getTime(), y: r.vo2max })), ...lineStyle(t.s1), stepped: true, tension: 0 }] }, options: o });
  }

  const wz = d.weekly_zones;
  const zo = baseOptions(t, { stacked: true, xFmt: fmt.weekLabel, yFmt: (v) => v + "h" });
  zo.plugins.tooltip.callbacks = {
    title: (it) => `Week of ${fmt.weekLabel(wz[it[0].dataIndex].week)}`,
    label: (it) => ` ${it.dataset.label}: ${fmt.hours(it.parsed.y * 3600)}`,
  };
  makeChart("c-zones", {
    type: "bar",
    data: {
      labels: wz.map((w) => w.week),
      datasets: [0, 1, 2, 3, 4].map((z) => ({
        label: `Z${z + 1}`,
        data: wz.map((w) => +(w.seconds[z] / 3600).toFixed(2)),
        backgroundColor: t.zones[z], hoverBackgroundColor: t.zones[z],
        borderColor: t.surface, borderWidth: { top: 2, bottom: 0, left: 0, right: 0 },
        maxBarThickness: 24, borderSkipped: "start",
        borderRadius: z === 4 ? { topLeft: 4, topRight: 4 } : 0,
      })),
    },
    options: zo,
  });
}

const trendLegend = () => `<div class="legend">
  <span><i class="key" style="background:var(--muted);border-radius:50%"></i>Run</span>
  <span><i class="key line" style="background:var(--series-1)"></i>10-run avg</span></div>`;

/* ================================================================ records */

async function renderRecords() {
  const d = await api("/api/records");
  const anyBest = d.bests.some((b) => b.top.length);
  if (!anyBest) { view.innerHTML = `<div class="page-head"><h1>Records</h1></div>${emptyState()}`; return; }
  view.innerHTML = `
    <div class="page-head"><div><h1>Records</h1><div class="sub">Fastest segments found inside your runs (from the watch's distance data)</div></div></div>
    <div class="card">
      <div class="card-head"><h2>Race predictions</h2><span class="sub">Riegel formula, from your best efforts of the last ${d.window_days} days</span></div>
      <div class="tiles">
        ${d.predictions.map((p) => `<div class="tile">
          <div class="label">${esc(p.label)}</div>
          <div class="value">${p.time_s ? fmt.dur(p.time_s) : "–"}</div>
          <div class="delta">${p.time_s ? `${fmt.pace(p.time_s / p.distance_m * 1000)} /km · from ${esc(p.from_label)} <a href="#/activity/${p.from_id}">${fmt.dur(p.from_time_s)}</a>` : "no recent efforts"}</div>
        </div>`).join("")}
      </div>
    </div>
    <div class="grid cols-2">
      ${d.bests.filter((b) => b.top.length).map((b) => `
        <div class="card">
          <div class="card-head"><h2>${esc(b.label)}</h2><span class="pill pr">${icons.star} ${fmt.dur(b.top[0].time_s)}</span></div>
          <div class="table-wrap"><table><tbody>
            ${b.top.map((e, i) => `<tr class="link" data-href="#/activity/${e.id}">
              <td class="muted">${i + 1}</td><td class="r">${fmt.dur(e.time_s)}</td>
              <td class="r">${fmt.pace(e.time_s / b.distance_m * 1000)} /km</td>
              <td>${fmt.date(e.local_date)}</td><td class="act-name">${esc(e.name)}</td></tr>`).join("")}
          </tbody></table></div>
        </div>`).join("")}
    </div>`;
}

/* ================================================================ heatmap */

async function renderHeatmap() {
  const lines = await api("/api/heatmap");
  view.innerHTML = `
    <div class="page-head"><div><h1>Heatmap</h1><div class="sub">${lines.length} runs with GPS</div></div></div>
    <div class="card" style="padding:8px"><div class="map full" id="hmap"></div></div>`;
  const m = L.map("hmap");
  maps.push(m);
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19, attribution: "&copy; OpenStreetMap contributors",
  }).addTo(m);
  if (!lines.length) { m.setView([46.5, 2.5], 5); return; }
  const pts = lines.flat();
  L.heatLayer(pts, {
    radius: 6, blur: 8, minOpacity: 0.35, max: 1,
    gradient: { 0.2: "#9ec5f4", 0.5: "#3987e5", 0.8: "#1c5cab", 1.0: "#0d366b" },
  }).addTo(m);
  m.fitBounds(L.latLngBounds(pts), { padding: [20, 20] });
}

/* ================================================================ settings */

async function renderSettings() {
  const s = await api("/api/settings");
  const q = new URLSearchParams(location.hash.split("?")[1] || "");
  const flash = {
    connected: ["ok", "Strava connected."],
    denied: ["err", "Strava authorisation was cancelled or the state did not match."],
    scope: ["err", "Strava connected without upload permission - reconnect and keep “Upload your activities” ticked."],
    failed: ["err", "Could not exchange the Strava code - check STRAVA_CLIENT_ID / SECRET in .env."],
  }[q.get("strava")];
  const sv = s.strava;
  const ls = s.last_scan;
  const rp = s.reprocess;
  view.innerHTML = `
    <div class="page-head"><h1>Settings</h1></div>
    ${flash ? `<div class="notice ${flash[0]}">${flash[0] === "ok" ? icons.check : icons.alert}${flash[1]}</div>` : ""}
    <div class="grid cols-2">
      <div class="card">
        <div class="card-head"><h2>Strava</h2></div>
        ${!sv.configured ? `<p class="sub">Set <code>STRAVA_CLIENT_ID</code> and <code>STRAVA_CLIENT_SECRET</code> in <code>.env</code>, then restart the container.</p>`
          : sv.connected ? `<p>Connected${sv.athlete ? ` as <strong>${esc(sv.athlete.name)}</strong>` : ""}.</p>
              <button id="strava-off">Disconnect</button>`
          : `<p class="sub">Authorise this app to upload activities to your Strava account.</p>
              <a class="btn strava" href="/strava/connect">${icons.upload} Connect Strava</a>`}
      </div>
      <div class="card">
        <div class="card-head"><h2>Import</h2><button id="scan">${icons.refresh} Scan now</button></div>
        <dl class="kv">
          <dt>Watched folder</dt><dd><code>${esc(s.watch_dir)}</code>${s.watch_dir_exists ? "" : ` <span class="pill err">${icons.alert} not found</span>`}</dd>
          <dt>Scan interval</dt><dd>every ${s.scan_interval} s</dd>
          <dt>Last scan</dt><dd>${ls ? `${fmt.dateTime(ls.at)} · ${ls.imported} new, ${ls.skipped} skipped, ${ls.error} errors` : "never"}</dd>
        </dl>
      </div>
      <div class="card">
        <div class="card-head"><h2>Heart rate zones</h2><button id="reprocess">${icons.refresh} Recalculate all</button></div>
        <dl class="kv">
          <dt>Max HR / resting HR</dt><dd>${s.max_hr} / ${s.resting_hr} bpm</dd>
          ${s.zone_limits.map((lo, i) => `<dt><i class="key" style="background:var(--z${i + 1})"></i> Zone ${i + 1}</dt><dd>${lo}${i < 4 ? "–" + (s.zone_limits[i + 1] - 1) : "+"} bpm</dd>`).join("")}
          <dt>Timezone</dt><dd>${esc(s.tz)}</dd>
        </dl>
        <p class="sub">Change <code>MAX_HR</code> / <code>RESTING_HR</code> in <code>.env</code>, restart, then “Recalculate all” to update zones and load.
        ${rp ? `<br>Last recalculation: ${rp.done}/${rp.total}${rp.failed ? `, ${rp.failed} failed` : ""}.` : ""}</p>
      </div>
    </div>
    <div class="card">
      <div class="card-head"><h2>Import log</h2><span class="sub">latest 50 files</span></div>
      ${s.import_log.length ? `<div class="table-wrap"><table>
        <thead><tr><th>File</th><th>Status</th><th>Detail</th><th>When</th></tr></thead>
        <tbody>${s.import_log.map((r) => `<tr><td>${esc(r.path.split("/").pop())}</td>
          <td><span class="pill ${r.status === "imported" ? "good" : r.status === "error" ? "err" : ""}">${esc(r.status)}</span></td>
          <td class="muted">${esc(r.message)}</td><td class="muted">${fmt.dateTime(r.seen_at.replace(" ", "T") + "Z")}</td></tr>`).join("")}</tbody>
      </table></div>` : `<p class="sub">No files seen yet.</p>`}
    </div>`;

  $("#scan").onclick = async (e) => {
    e.target.disabled = true;
    try {
      const r = await api("/api/scan", { method: "POST" });
      toast(`${r.imported} imported, ${r.duplicate} duplicates, ${r.skipped} skipped, ${r.error} errors`);
      route();
    } catch (err) { toast(err.message); e.target.disabled = false; }
  };
  $("#reprocess").onclick = async (e) => {
    e.target.disabled = true;
    try { await api("/api/reprocess", { method: "POST" }); toast("Recalculating in the background…"); }
    catch (err) { toast(err.message); }
  };
  const off = $("#strava-off");
  if (off) off.onclick = async () => {
    if (!confirm("Disconnect Strava? Already uploaded activities stay on Strava.")) return;
    await api("/api/strava/disconnect", { method: "POST" });
    route();
  };
}

route();
