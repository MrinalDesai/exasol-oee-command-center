"use strict";

/* ============ constants ============ */
const PLANTS = { PNQ: "Pune", BLR: "Bengaluru", CHN: "Chennai" };
const PLANT_ORDER = ["PNQ", "BLR", "CHN"];
const TYPES = { CNC_MILL: "CNC mill", CNC_LATHE: "CNC lathe", PRESS: "Press", PACKER: "Packer" };
const TYPES_LOWER = { CNC_MILL: "CNC mill", CNC_LATHE: "CNC lathe", PRESS: "press", PACKER: "packer" };
const TYPES_PLURAL = { CNC_MILL: "CNC mills", CNC_LATHE: "CNC lathes", PRESS: "Presses", PACKER: "Packers" };
const MODES = ["TOOL_WEAR", "OVERSTRAIN", "HEAT_DISSIPATION", "POWER", "BEARING"];
const MODE_NAME = { TOOL_WEAR: "Tool wear", HEAT_DISSIPATION: "Heat dissipation", POWER: "Power", OVERSTRAIN: "Overstrain", BEARING: "Bearing" };
const MODE_COLOR = { TOOL_WEAR: "#ef4444", OVERSTRAIN: "#f59e0b", HEAT_DISSIPATION: "#38bdf8", POWER: "#a78bfa", BEARING: "#facc15" };
const ACTIONS = {
  TOOL_WEAR: "Replace the cutting insert (TOOL-INSERT-T12) in the next planned window and check surface finish on the last batch.",
  HEAT_DISSIPATION: "Flush the coolant circuit and check flow; replace the coolant pump (COOLANT-PUMP-CP4) if it is weak.",
  POWER: "Inspect the spindle drive (SPINDLE-DRIVE-SD2) and supply; review the drive fault log.",
  OVERSTRAIN: "Ease the load or change worn tooling, and inspect the gearbox seal (GEARBOX-SEAL-GS7) for leaks.",
  BEARING: "Schedule a bearing change (BEARING-6205) and check alignment while the machine is down.",
};
const SENSORS = [
  { key: "VIBRATION_MM_S", label: "Vibration (mm/s)", short: "Vibration", unit: "mm/s", d: 2 },
  { key: "TOOL_WEAR_MIN", label: "Tool wear (min)", short: "Tool wear", unit: "min", d: 0 },
  { key: "TEMP_DIFF_K", label: "Temp gap (K)", short: "Process − air temp", unit: "K", d: 1 },
  { key: "TORQUE_RATIO", label: "Torque ratio", short: "Torque vs nominal", unit: "×", d: 2 },
  { key: "RISK_24H", label: "Failure risk", short: "24 h failure risk", unit: "", d: 0, pct: true },
];
const LAB = [
  { key: "TOOL_WEAR_MIN", label: "Tool wear", unit: "min", min: 0, max: 350, step: 1, d: 0 },
  { key: "VIBRATION_MM_S", label: "Vibration", unit: "mm/s", min: 1, max: 8, step: 0.05, d: 2 },
  { key: "TEMP_DIFF_K", label: "Process − air temperature", unit: "K", min: 4, max: 14, step: 0.1, d: 1 },
  { key: "TORQUE_RATIO", label: "Torque vs nominal", unit: "×", min: 0.6, max: 1.8, step: 0.01, d: 2 },
];
const WINDOW = 168; // hours shown in the feed
const SHIFT_HOURS_PER_DAY = 22.5; // 3 shifts × 7.5 h planned
const CRITICAL = 0.5, WATCH = 0.10;
const STATUS_COLOR = { healthy: "#22c55e", watch: "#f59e0b", critical: "#ef4444", unknown: "#4a5a6a" };

/* ============ state ============ */
const S = {
  backend: "Exasol", fleet: [], lines: [], motor: null, sensor: "VIBRATION_MM_S",
  series: [], stops: [], diag: null, winEnd: null, histMode: null, labBase: null, labVals: {},
};
const boot_t = Date.now();

/* ============ helpers ============ */
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const pct = (v, d = 0) => (v == null || isNaN(v) ? "—" : (v * 100).toFixed(d) + "%");
const num = (v, d = 0) => (v == null || isNaN(v) ? "—" : Number(v).toLocaleString("en-IN", { minimumFractionDigits: d, maximumFractionDigits: d }));
const ts = (s) => new Date(String(s).replace(" ", "T"));
const dShort = (d) => d.toLocaleDateString("en-GB", { day: "numeric", month: "short" });
const hhmm = (d) => d.toTimeString().slice(0, 5);
const dTime = (d) => dShort(d) + " " + hhmm(d);
const lineLabel = (lineId) => `${PLANTS[lineId.slice(0, 3)] || lineId.slice(0, 3)} · L${lineId.split("-L")[1]}`;
const fmtSensor = (s, v) => (s.pct ? pct(v) : num(v, s.d) + (s.unit ? " " + s.unit : ""));
const big = (v) => (v == null || isNaN(v) ? "—" : v >= 1e6 ? (v / 1e6).toFixed(2) + " M" : v >= 1e3 ? (v / 1e3).toFixed(1) + " k" : num(v));
const sigma = (z) => (z == null ? "" : (z >= 0 ? "+" : "−") + Math.abs(z).toFixed(1) + "σ");
const woName = (id) => "WO-" + String(Math.round(id)).padStart(4, "0");

async function api(path, opts) {
  const r = await fetch(path, opts);
  let body = {};
  try { body = await r.json(); } catch { /* not JSON */ }
  if (!r.ok) throw new Error(typeof body.detail === "string" ? body.detail : `Request failed (${r.status})`);
  return body;
}

function sqlNote(key, list) {
  const el = document.querySelector(`.sqlnote[data-sql="${key}"]`);
  if (!el) return;
  const qs = (Array.isArray(list) ? list : [list]).filter((q) => q && q.sql);
  if (!qs.length) { el.innerHTML = ""; return; }
  const ms = qs.reduce((a, q) => a + (q.ms || 0), 0);
  const sql = qs.map((q) => q.sql.trim()).join(";\n\n") + ";";
  const wasOpen = !!el.querySelector("details")?.open;
  el.innerHTML = `<details${wasOpen ? " open" : ""}><summary><span class="db">⛁ ${esc(S.backend)}</span><span>${qs.length} ${qs.length === 1 ? "query" : "queries"} · ${num(ms)} ms</span><u>${wasOpen ? "hide" : "show"} SQL</u></summary><pre>${esc(sql)}</pre></details>`;
  const det = el.querySelector("details");
  det.addEventListener("toggle", () => { det.querySelector("u").textContent = (det.open ? "hide" : "show") + " SQL"; });
}

const tip = $("tip");
function showTip(html, x, y) {
  tip.innerHTML = html; tip.hidden = false;
  const r = tip.getBoundingClientRect();
  let l = x + 14, t = y + 14;
  if (l + r.width > innerWidth - 8) l = x - r.width - 14;
  if (t + r.height > innerHeight - 8) t = y - r.height - 14;
  tip.style.left = l + "px"; tip.style.top = t + "px";
}
const hideTip = () => { tip.hidden = true; };

function sizeCanvas(c) {
  const dpr = window.devicePixelRatio || 1;
  const w = c.clientWidth, h = c.clientHeight;
  if (c.width !== Math.round(w * dpr) || c.height !== Math.round(h * dpr)) { c.width = Math.round(w * dpr); c.height = Math.round(h * dpr); }
  const x = c.getContext("2d");
  x.setTransform(dpr, 0, 0, dpr, 0, 0);
  return { x, W: w, H: h };
}

/* ============ top bar ============ */
setInterval(() => {
  const s = Math.floor((Date.now() - boot_t) / 1000);
  $("uptime").textContent = `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m ${s % 60}s`;
}, 1000);

async function loadHealth() {
  try {
    const h = await api("/api/health");
    S.backend = h.backend || "Exasol";
    const r = h.rows[0];
    $("sys").textContent = "ONLINE"; $("sys").className = "v on";
    $("asof").textContent = dTime(ts(r.LAST_TS));
    $("scored").textContent = num(r.SCORED_ROWS);
    $("scored").title = `${num(r.SCORED_ROWS)} readings scored by OEE.FAILURE_RISK on ${S.backend}`;
    return true;
  } catch (e) {
    $("sys").textContent = "OFFLINE"; $("sys").className = "v off";
    $("main").insertAdjacentHTML("afterbegin", `<div class="card err" style="margin-bottom:14px">Can't reach the database: ${esc(e.message)}. Check that Exasol is running and EXA_DSN in .env is correct, then reload.</div>`);
    return false;
  }
}

/* ============ fleet: floor, alerts, pipeline ============ */
async function loadFleet() {
  const f = await api("/api/fleet");
  S.fleet = f.machines;
  sqlNote("fleet", f.queries);
  renderFloor(); renderAlerts(); renderPipes(); renderOEE();
}

function machineSVG(type, color) {
  const body = `fill="#0a1018" stroke="${color}" stroke-width="2"`;
  const fin = `stroke="${color}" stroke-width="1" opacity=".4"`;
  const g = {
    CNC_MILL: `<rect x="14" y="20" width="50" height="24" rx="4" ${body}/><rect x="32" y="4" width="14" height="18" rx="2" ${body}/><line x1="39" y1="22" x2="39" y2="30" stroke="${color}" stroke-width="2"/><line x1="20" y1="36" x2="58" y2="36" ${fin}/><rect x="20" y="44" width="10" height="5" fill="${color}" opacity=".7"/><rect x="48" y="44" width="10" height="5" fill="${color}" opacity=".7"/>`,
    CNC_LATHE: `<rect x="6" y="22" width="66" height="18" rx="4" ${body}/><rect x="8" y="12" width="16" height="26" rx="3" ${body}/><circle cx="16" cy="25" r="4" fill="${color}"/><rect x="56" y="16" width="12" height="10" rx="2" ${body}/><line x1="28" y1="31" x2="54" y2="31" ${fin}/><rect x="14" y="40" width="10" height="7" fill="${color}" opacity=".7"/><rect x="54" y="40" width="10" height="7" fill="${color}" opacity=".7"/>`,
    PRESS: `<rect x="16" y="4" width="46" height="10" rx="3" ${body}/><rect x="18" y="14" width="6" height="30" ${body}/><rect x="54" y="14" width="6" height="30" ${body}/><rect x="28" y="16" width="22" height="12" rx="2" fill="${color}" opacity=".85"/><rect x="24" y="36" width="30" height="8" rx="2" ${body}/><rect x="14" y="44" width="50" height="5" fill="${color}" opacity=".7"/>`,
    PACKER: `<rect x="22" y="8" width="34" height="24" rx="4" ${body}/><rect x="4" y="32" width="70" height="8" rx="4" ${body}/><circle cx="12" cy="36" r="2.5" fill="${color}"/><circle cx="66" cy="36" r="2.5" fill="${color}"/><rect x="30" y="16" width="18" height="8" rx="1" fill="${color}" opacity=".5"/><rect x="14" y="40" width="8" height="8" fill="${color}" opacity=".7"/><rect x="56" y="40" width="8" height="8" fill="${color}" opacity=".7"/>`,
  }[type] || `<rect x="6" y="12" width="66" height="30" rx="6" ${body}/>`;
  return `<svg width="78" height="50" viewBox="0 0 78 50" aria-hidden="true">${g}</svg>`;
}

function renderFloor() {
  const byLine = {};
  S.fleet.forEach((m) => (byLine[m.line_id] = byLine[m.line_id] || []).push(m));
  const order = Object.keys(byLine).sort((a, b) => PLANT_ORDER.indexOf(a.slice(0, 3)) - PLANT_ORDER.indexOf(b.slice(0, 3)) || a.localeCompare(b));
  $("floor").innerHTML =
    `<div class="floorkey"><span><i style="background:#22c55e"></i>Healthy, risk under 10%</span><span><i style="background:#f59e0b"></i>Watch, 10–50%</span><span><i style="background:#ef4444"></i>Critical, 50% or more</span></div>` +
    order.map((ln) => `<div class="lineRow"><span class="lineTag"><b>${esc(PLANTS[ln.slice(0, 3)])}</b>Line L${esc(ln.split("-L")[1])}</span>${byLine[ln].map((m) => {
      const tag = m.status === "critical" ? `<span class="fault critical">${esc(MODE_NAME[m.likely_modes[0].mode].toUpperCase())} ${pct(m.risk)}</span>`
        : m.status === "watch" ? `<span class="fault watch">RISK ${pct(m.risk)}</span>` : "";
      return `<button type="button" class="machine${m.machine_id === S.motor ? " sel" : ""}" data-id="${esc(m.machine_id)}" aria-label="${esc(m.machine_id)}, ${esc(TYPES[m.machine_type])}, ${m.status}, risk ${pct(m.risk)}">${tag}<span class="g-${m.status}">${machineSVG(m.machine_type, STATUS_COLOR[m.status])}</span><div class="mid">${esc(m.machine_id)}</div></button>`;
    }).join("")}</div>`).join("");
  $("floor").querySelectorAll(".machine").forEach((b) => b.addEventListener("click", () => selectMachine(b.dataset.id)));
}

function alerts() {
  return S.fleet.filter((m) => m.status === "critical" || m.status === "watch").sort((a, b) => b.risk - a.risk);
}

function renderAlerts() {
  const al = alerts();
  $("acount").textContent = al.length;
  const shown = al.slice(0, 5);
  $("ticker").innerHTML = `<span class="hd">ACTIVE ALERTS (${al.length}) ▲</span>` + (al.length ? shown.map((m) =>
    `<button type="button" class="item" data-id="${esc(m.machine_id)}"><span class="dot">●</span><span class="mono">${esc(hhmm(ts(m.reading_ts)))}</span><b class="mono">${esc(m.machine_id)}</b><span>${esc(MODE_NAME[m.likely_modes[0].mode].toLowerCase())} risk ${pct(m.risk)}</span><span class="sev ${m.status === "critical" ? "HIGH" : "MEDIUM"}">${m.status === "critical" ? "HIGH" : "MEDIUM"}</span></button>`).join("")
    : `<span class="muted">No machines above 10% risk.</span>`) + (al.length > shown.length ? `<a class="muted" href="#pipesect">+${al.length - shown.length} more</a>` : "");
  $("ticker").querySelectorAll(".item").forEach((b) => b.addEventListener("click", () => { selectMachine(b.dataset.id); $("main").scrollTo({ top: 0 }); }));
}

function renderPipes() {
  const al = alerts();
  if (!al.length) { $("pipes").innerHTML = `<span class="muted">No active faults. Plant nominal.</span>`; return; }
  const shownP = al.slice(0, 6);
  $("pipes").innerHTML = shownP.map((m) => {
    const mode = m.likely_modes[0].mode;
    const partsOk = m.part.on_hand == null || m.part.on_hand > 0;
    const wo = m.work_order;
    const nodes = [
      { t: "DETECT", cls: "done", s: pct(m.risk) },
      { t: "DIAGNOSE", cls: "done", s: MODE_NAME[mode].toLowerCase() },
      { t: "PARTS", cls: partsOk ? "done" : "blocked", s: partsOk ? `${num(m.part.on_hand)} in stock` : `0 in stock · ${num(m.part.lead_time_days)} d lead` },
      { t: "WORK ORDER", cls: wo ? "done" : partsOk ? "now" : "", s: wo ? woName(wo.WO_ID) : partsOk ? "ready to raise" : "waiting on parts" },
    ];
    const flow = nodes.map((n, i) => (i ? `<div class="edge${nodes[i - 1].cls === "done" ? " lit" : ""}"></div>` : "")
      + `<div class="node ${n.cls}">${n.t}${n.cls === "done" ? '<span class="tick">✓</span>' : ""}<small>${esc(n.s)}</small></div>`).join("");
    const btn = !wo ? `<button type="button" class="qbtn${partsOk ? " go" : ""}" data-wo="${esc(m.machine_id)}">Raise work order</button>` : "";
    return `<div class="pipe"><div class="who"><b>${esc(m.machine_id)}</b><span>${esc(MODE_NAME[mode])}, ${esc(lineLabel(m.line_id))}</span><span class="sev ${m.status === "critical" ? "HIGH" : "MEDIUM"}">${m.status === "critical" ? "HIGH" : "MEDIUM"}</span>${btn}</div><div class="flow">${flow}</div></div>`;
  }).join("") + (al.length > shownP.length ? `<div class="muted">${al.length - shownP.length} more machines on watch; select them on the plant floor.</div>` : "");
  $("pipes").querySelectorAll("[data-wo]").forEach((b) => b.addEventListener("click", () => raiseWO(b.dataset.wo, b)));
}

async function raiseWO(id, btn) {
  if (btn) { btn.disabled = true; btn.textContent = "Writing to " + S.backend + "…"; }
  try {
    await api("/api/workorders", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ machine_id: id }) });
    await loadFleet();
    if (id === S.motor) await loadDiagnosis();
    loadSourcing(); loadSuppliers();
  } catch (e) {
    if (btn) { btn.disabled = false; btn.textContent = e.message; }
  }
}
async function closeWO(woId) {
  await api(`/api/workorders/${woId}/close`, { method: "POST" });
  await loadFleet(); await loadDiagnosis();
  loadSourcing(); loadSuppliers();
}

/* ============ sensor feed ============ */
function renderTabs() {
  $("sensortabs").innerHTML = SENSORS.map((s) => `<button type="button" role="tab" aria-selected="${s.key === S.sensor}" data-k="${s.key}">${esc(s.label)}</button>`).join("");
  $("sensortabs").querySelectorAll("button").forEach((b) => b.addEventListener("click", () => { S.sensor = b.dataset.k; renderTabs(); renderDetails(); drawFeed(true); }));
}

function sensorDef() { return SENSORS.find((s) => s.key === S.sensor); }
function baselineFor(key) {
  if (key === "RISK_24H") return CRITICAL;
  const m = S.fleet.find((x) => x.machine_id === S.motor);
  return m && m.signals[key] ? m.signals[key].baseline : null;
}

function windowSlice() {
  const n = S.series.length;
  const end = S.winEnd == null ? n : Math.min(Math.max(S.winEnd, Math.min(WINDOW, n)), n);
  return { a: Math.max(0, end - WINDOW), b: end };
}

let anim = null;
function drawFeed(animate = false) {
  if (!S.series.length) return;
  const s = sensorDef();
  const { a, b } = windowSlice();
  const pts = S.series.slice(a, b);
  const vals = pts.map((p) => p[s.key]);
  const base = baselineFor(s.key);
  const cur = vals[vals.length - 1];
  const peak = Math.max(...vals), avg = vals.reduce((x, y) => x + y, 0) / vals.length;
  const m = S.fleet.find((x) => x.machine_id === S.motor);

  $("feedsub").textContent = `${S.motor} · ${pts.length} hourly readings ending ${dTime(ts(pts[pts.length - 1].READING_TS))}`;
  $("legend").innerHTML = `<span class="lg">— <b>${esc(s.label)}</b></span><span>Current: <b>${fmtSensor(s, cur)}</b></span><span>Peak: <b>${fmtSensor(s, peak)}</b></span><span>Avg: <b>${fmtSensor(s, avg)}</b></span>`
    + `<span class="hint">red: readings scored at 50%+ risk · dashed: ${s.key === "RISK_24H" ? "alert threshold" : esc(TYPES[m?.machine_type] || "type") + " normal"} · click the strip below to move the window</span>`;

  const tail = (k) => S.series.slice(Math.max(0, b - 40), b).map((p) => p[k]);
  const cards = [
    ["Current", fmtSensor(s, cur), tail(s.key), "#22c55e", false],
    ["Peak, 7 d", fmtSensor(s, peak), tail(s.key), "#22c55e", false],
    ["Avg, 7 d", fmtSensor(s, avg), tail(s.key), "#22c55e", false],
    [s.key === "RISK_24H" ? "Alert threshold" : "Type normal", base == null ? "—" : fmtSensor(s, base), tail(s.key), "#3b9eff", false],
    ["24 h failure risk", pct(m?.risk), tail("RISK_24H"), "#ef4444", (m?.risk ?? 0) >= CRITICAL],
  ];
  $("cards").innerHTML = cards.map((c, i) => `<div class="stat"><div><div class="k">${esc(c[0])}</div><div class="v${c[4] ? " red" : ""}">${esc(c[1])}</div></div><canvas class="spark" id="sp${i}"></canvas></div>`).join("");
  cards.forEach((c, i) => drawSpark($("sp" + i), c[2], c[3]));

  const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (anim) cancelAnimationFrame(anim);
  drawScrub();
  if (!animate || reduce) { paintWave(pts, s, base, 1); return; }
  const t0 = performance.now();
  const step = (now) => {
    const p = Math.min(1, (now - t0) / 900);
    paintWave(pts, s, base, p);
    anim = p < 1 ? requestAnimationFrame(step) : null;
  };
  anim = requestAnimationFrame(step);
}

let waveGeom = null;
function paintWave(pts, s, base, progress) {
  const c = $("wave");
  const { x, W, H } = sizeCanvas(c);
  const L = 54, R = 12, T = 12, B = 24;
  const w = W - L - R, h = H - T - B;
  const vals = pts.map((p) => p[s.key]);
  let lo, hi;
  if (s.pct) { lo = 0; hi = 1; } else {
    lo = Math.min(...vals, base ?? Infinity); hi = Math.max(...vals, base ?? -Infinity);
    const pad = (hi - lo) * 0.12 || 1; lo -= pad; hi += pad;
    if (s.key === "TOOL_WEAR_MIN") lo = Math.max(0, lo);
  }
  const n = pts.length;
  const X = (i) => L + (i / Math.max(n - 1, 1)) * w;
  const Y = (v) => T + h - ((v - lo) / (hi - lo)) * h;
  const t0 = ts(pts[0].READING_TS).getTime(), t1 = ts(pts[n - 1].READING_TS).getTime();
  const Xt = (t) => L + ((t - t0) / Math.max(t1 - t0, 1)) * w;
  waveGeom = { pts, s, X, Y, L, T, w, h, n };
  x.clearRect(0, 0, W, H);

  // grid
  x.strokeStyle = "#16202c"; x.lineWidth = 0.6; x.setLineDash([3, 4]);
  for (let i = 1; i < 8; i++) { x.beginPath(); x.moveTo(L, T + (i * h) / 8); x.lineTo(L + w, T + (i * h) / 8); x.stroke(); }
  for (let i = 1; i < 12; i++) { x.beginPath(); x.moveTo(L + (i * w) / 12, T); x.lineTo(L + (i * w) / 12, T + h); x.stroke(); }
  x.setLineDash([]);
  x.fillStyle = "#4a5a6a"; x.font = "10px 'JetBrains Mono', monospace"; x.textAlign = "right";
  for (let i = 0; i <= 4; i++) { const v = lo + ((hi - lo) * i) / 4; x.fillText(s.pct ? pct(v) : num(v, s.d > 0 ? 1 : 0), L - 8, Y(v) + 3); }
  x.textAlign = "center";
  const day = 86400000;
  for (let t = Math.ceil(t0 / day) * day; t <= t1; t += day) {
    const xx = Xt(t); if (xx < L + 20 || xx > L + w - 20) continue;
    x.fillText(dShort(new Date(t)), xx, H - 6);
  }

  // stops in window
  S.stops.forEach((st) => {
    const a = ts(st.START_TS).getTime(), b = ts(st.END_TS).getTime();
    if (b < t0 || a > t1) return;
    const xa = Math.max(Xt(a), L), xb = Math.min(Xt(b), L + w);
    const un = st.STOP_TYPE === "UNPLANNED";
    x.fillStyle = un ? "rgba(239,68,68,.16)" : "rgba(132,148,167,.12)";
    x.fillRect(xa, T, Math.max(xb - xa, 2), h);
    x.fillStyle = un ? "#ef4444" : "#8494a7"; x.font = "bold 9.5px 'JetBrains Mono', monospace"; x.textAlign = "left";
    x.fillText(un ? "STOP" : "PM", xa + 3, T + h - 6);
  });

  // high-risk ranges
  const hot = pts.map((p) => p.RISK_24H >= CRITICAL);
  const ranges = [];
  for (let i = 0; i < n; i++) if (hot[i]) { if (ranges.length && ranges[ranges.length - 1][1] === i - 1) ranges[ranges.length - 1][1] = i; else ranges.push([i, i]); }
  ranges.forEach(([i0, i1]) => {
    const xa = X(i0), xb = Math.max(X(i1), xa + 6);
    x.fillStyle = "rgba(239,68,68,.10)"; x.fillRect(xa, T, xb - xa, h);
    x.strokeStyle = "#ef4444"; x.setLineDash([6, 5]); x.lineWidth = 1.4; x.strokeRect(xa, T + 1, xb - xa, h - 2); x.setLineDash([]);
  });
  const banner = $("banner");
  if (ranges.length) {
    const [i0, i1] = ranges[ranges.length - 1];
    const peakRisk = Math.max(...pts.slice(i0, i1 + 1).map((p) => p.RISK_24H));
    const m = S.fleet.find((q) => q.machine_id === S.motor);
    const live = i1 === n - 1 && S.winEnd == null && m;
    banner.textContent = live ? `⚠ ${MODE_NAME[m.likely_modes[0].mode].toUpperCase()} — ${pct(m.risk)} RISK, NEXT 24 H` : `⚠ HIGH FAILURE RISK — PEAK ${pct(peakRisk)}`;
    banner.hidden = false;
    banner.style.left = Math.max(L, Math.min(X(i0) + 6, W - banner.offsetWidth - 12)) + "px";
  } else banner.hidden = true;

  // baseline / threshold line (its label is drawn last, on top of the series)
  const showBase = base != null && base >= lo && base <= hi;
  if (showBase) {
    x.strokeStyle = "#3b9eff"; x.globalAlpha = 0.55; x.setLineDash([2, 4]); x.lineWidth = 1;
    x.beginPath(); x.moveTo(L, Y(base)); x.lineTo(L + w, Y(base)); x.stroke(); x.setLineDash([]); x.globalAlpha = 1;
  }

  // series, revealed left to right by progress; blue pass then red pass
  const last = Math.max(1, Math.floor(progress * (n - 1)));
  const pass = (wantHot, color, width, glow) => {
    x.strokeStyle = color; x.lineWidth = width; x.shadowColor = color; x.shadowBlur = glow;
    x.beginPath();
    let pen = false;
    for (let i = 1; i <= last; i++) {
      const segHot = hot[i] || hot[i - 1];
      if (segHot !== wantHot) { pen = false; continue; }
      if (!pen) x.moveTo(X(i - 1), Y(vals[i - 1]));
      x.lineTo(X(i), Y(vals[i]));
      pen = true;
    }
    x.stroke(); x.shadowBlur = 0;
  };
  pass(false, "#3b9eff", 1.6, 4);
  pass(true, "#ef4444", 2, 8);
  if (showBase) {
    const label = s.pct ? "ALERT 50%" : `TYPE NORMAL ${fmtSensor(s, base)}`;
    x.font = "bold 10px 'JetBrains Mono', monospace";
    const tw = x.measureText(label).width;
    const lx = L + 8, ly = Y(base) - 6;
    x.fillStyle = "rgba(13,20,29,.92)"; x.fillRect(lx - 4, ly - 11, tw + 8, 15);
    x.strokeStyle = "rgba(59,158,255,.5)"; x.lineWidth = 1; x.strokeRect(lx - 4 + .5, ly - 11 + .5, tw + 7, 14);
    x.fillStyle = "#6fb4ff"; x.textAlign = "left"; x.fillText(label, lx, ly);
  }
  if (progress < 1) {
    const hx = X(last), hy = Y(vals[last]);
    const g = x.createRadialGradient(hx, hy, 0, hx, hy, 10);
    g.addColorStop(0, "rgba(59,158,255,.95)"); g.addColorStop(1, "rgba(0,0,0,0)");
    x.fillStyle = g; x.beginPath(); x.arc(hx, hy, 10, 0, 7); x.fill();
  }
}

function drawFeedStatic() { if (waveGeom) paintWave(waveGeom.pts, waveGeom.s, baselineFor(waveGeom.s.key), 1); }
$("wave").addEventListener("mousemove", (ev) => {
  if (!waveGeom || anim) return;
  const { pts, s, X, Y, L, T, w, h, n } = waveGeom;
  const r = $("wave").getBoundingClientRect();
  const i = Math.round(((ev.clientX - r.left - L) / w) * (n - 1));
  if (i < 0 || i >= n) { hideTip(); drawFeedStatic(); return; }
  drawFeedStatic();
  const x = $("wave").getContext("2d");
  x.strokeStyle = "#8494a7"; x.setLineDash([3, 3]); x.lineWidth = 1;
  x.beginPath(); x.moveTo(X(i), T); x.lineTo(X(i), T + h); x.stroke(); x.setLineDash([]);
  const p = pts[i];
  x.fillStyle = p.RISK_24H >= CRITICAL ? "#ef4444" : "#3b9eff";
  x.beginPath(); x.arc(X(i), Y(p[s.key]), 4, 0, 7); x.fill();
  showTip(`${esc(dTime(ts(p.READING_TS)))}<br>${esc(s.short)}: <b>${fmtSensor(s, p[s.key])}</b><br>24 h risk: <b style="color:${p.RISK_24H >= CRITICAL ? "#ef4444" : "#e8eef5"}">${pct(p.RISK_24H)}</b>`, ev.clientX, ev.clientY);
});
$("wave").addEventListener("mouseleave", () => { hideTip(); if (!anim) drawFeedStatic(); });

function drawScrub() {
  const c = $("scrub");
  const { x, W, H } = sizeCanvas(c);
  x.clearRect(0, 0, W, H);
  const n = S.series.length; if (!n) return;
  const s = sensorDef();
  const vals = S.series.map((p) => p[s.key]);
  const lo = Math.min(...vals), hi = Math.max(...vals), sp = hi - lo || 1;
  const X = (i) => (i / (n - 1)) * W;
  x.fillStyle = "rgba(239,68,68,.28)";
  S.series.forEach((p, i) => { if (p.RISK_24H >= CRITICAL) x.fillRect(X(i), 0, Math.max(W / n, 1), H); });
  x.beginPath();
  vals.forEach((v, i) => { const yy = H - ((v - lo) / sp) * (H - 6) - 3; i ? x.lineTo(X(i), yy) : x.moveTo(X(i), yy); });
  x.strokeStyle = "#2b6cb0"; x.lineWidth = 0.8; x.stroke();
  const { a, b } = windowSlice();
  x.fillStyle = "rgba(59,158,255,.10)"; x.fillRect(X(a), 0, X(b - 1) - X(a), H);
  x.strokeStyle = "#3b9eff"; x.lineWidth = 1; x.strokeRect(X(a) + 0.5, 0.5, X(b - 1) - X(a) - 1, H - 1);
}
$("scrub").addEventListener("click", (ev) => {
  const r = $("scrub").getBoundingClientRect();
  const n = S.series.length;
  const center = Math.round(((ev.clientX - r.left) / r.width) * (n - 1));
  const end = Math.min(n, Math.max(WINDOW, center + WINDOW / 2));
  S.winEnd = end >= n ? null : end;
  drawFeed(false);
});

function drawSpark(c, vals, color) {
  if (!c || !vals.length) return;
  const { x, W, H } = sizeCanvas(c);
  x.clearRect(0, 0, W, H);
  const lo = Math.min(...vals), hi = Math.max(...vals), sp = hi - lo || 1;
  x.beginPath();
  vals.forEach((v, i) => { const px = (i / Math.max(vals.length - 1, 1)) * W, py = H - ((v - lo) / sp) * (H - 4) - 2; i ? x.lineTo(px, py) : x.moveTo(px, py); });
  x.strokeStyle = color; x.lineWidth = 1.3; x.stroke();
}

/* ============ sidebar details ============ */
function renderDetails() {
  const m = S.fleet.find((x) => x.machine_id === S.motor);
  if (!m) return;
  $("d_id").textContent = m.machine_id;
  $("d_loc").textContent = lineLabel(m.line_id);
  $("d_type").textContent = TYPES[m.machine_type] || m.machine_type;
  $("d_param").textContent = sensorDef().label;
  $("d_risk").textContent = pct(m.risk);
  $("d_status").textContent = m.status.toUpperCase();
  $("d_status").style.color = STATUS_COLOR[m.status];
}

/* ============ diagnosis ============ */
async function loadDiagnosis() {
  const el = $("diag");
  try {
    const d = await api("/api/diagnosis/" + encodeURIComponent(S.motor));
    S.diag = d;
    sqlNote("diag", [...d.queries, d.similar, d.parts]);
    const m = d.machine;
    const top = m.likely_modes[0], second = m.likely_modes[1];
    const cls = m.status === "critical" ? "red" : m.status === "watch" ? "amber" : "";
    const dp = (f) => (f.unit === "min" ? 0 : 2);
    const factorText = m.top_factors.slice(0, 2).map((f) => `${f.label.toLowerCase()} is ${num(f.value, dp(f))} ${f.unit}, ${Math.abs(f.z).toFixed(1)}σ ${f.z >= 0 ? "above" : "below"} the ${TYPES_LOWER[m.machine_type]} normal of ${num(f.baseline, dp(f))}`).join("; ");
    const finding = m.status === "healthy"
      ? `${m.machine_id} is running normally, with a ${pct(m.risk)} risk of an unplanned stop in the next 24 hours. The largest deviations are small: ${factorText}. No action is needed beyond the regular maintenance plan.`
      : `${m.machine_id} has a ${pct(m.risk)} risk of an unplanned stop in the next 24 hours, based on the reading at ${dTime(ts(m.reading_ts))}. The strongest signals: ${factorText}. Matching its five sensor-stream patterns to the fault signatures learned from past failures, the most likely cause is ${MODE_NAME[top.mode].toLowerCase()} (${pct(top.p)}), then ${MODE_NAME[second.mode].toLowerCase()} (${pct(second.p)}); ${TYPES_PLURAL[m.machine_type].toLowerCase()} in this fleet have stopped this way ${top.history} time${top.history === 1 ? "" : "s"}.${m.unplanned_hours ? ` An unplanned stop of this kind averages ${num(m.unplanned_hours, 1)} h of downtime.` : ""}`;
    const wo = m.work_order;
    el.innerHTML = `<div class="diag"><div class="card verdict">
      <div class="k">Model verdict · ${esc(m.machine_id)}</div>
      <div class="big ${cls}">${pct(m.risk)}</div>
      <div class="cap">risk of an unplanned stop in the next 24 h</div>
      <div class="k">Likely failure mode</div>
      ${m.likely_modes.slice(0, 3).map((x) => `<div class="modebar"><span>${esc(MODE_NAME[x.mode])}</span><b>${pct(x.p)}</b><div class="track"><i style="width:${(x.p * 100).toFixed(1)}%;background:${MODE_COLOR[x.mode]}"></i></div></div>`).join("")}
      <div class="k" style="margin-top:14px">Why (vs ${esc(TYPES_LOWER[m.machine_type])} normal)</div>
      ${m.top_factors.map((f) => `<div class="factor"><span>${esc(f.label)}<br><small>${num(f.value, dp(f))} ${esc(f.unit)} · normal ${num(f.baseline, dp(f))}</small></span><span>${sigma(f.z)}</span></div>`).join("")}
    </div><div>
      <div class="card"><div class="k" style="margin-bottom:6px">Root-cause finding</div>
        <div class="finding">${esc(finding)}</div>
        ${m.status !== "healthy" ? `<div class="rec"><b>RECOMMENDED:</b> ${esc(ACTIONS[top.mode])}</div>` : ""}
      </div>
      <div class="subhead">Similar past repairs (technician notes)</div>
      <div class="sub3">${d.similar.rows.length ? d.similar.rows.map((r) => `<div class="card repair">
        <div class="hd"><span><b>STOP #${Math.round(r.STOP_ID)}</b> · ${esc(r.MACHINE_ID)}</span><span class="muted">${esc(dShort(ts(r.START_TS)))}</span></div>
        <div class="tx">${esc(r.NOTE_TEXT)}</div>
        <div class="ft">part: ${esc(r.PART_REPLACED || "—")} · downtime ${num(r.STOP_HOURS, 1)} h</div></div>`).join("")
        : `<span class="muted">No past repairs of this kind.</span>`}</div>
      <div class="actions">
        ${wo ? `<span class="msg">Work order <b style="color:#22c55e">${woName(wo.WO_ID)}</b> is open for ${esc(MODE_NAME[wo.FAILURE_MODE].toLowerCase())}.</span><button type="button" class="qbtn" id="woclose">Close ${woName(wo.WO_ID)}</button>`
          : m.status !== "healthy" ? `<button type="button" class="qbtn go" id="woraise">Raise work order</button><span class="msg">Writes a row to OEE.WORK_ORDERS in ${esc(S.backend)}.</span>` : ""}
        <button type="button" class="qbtn" id="golab">Try a fix in the What-if Lab</button>
      </div>
      <div class="subhead">Parts for repair · stock at ${esc(PLANTS[m.plant_id])}</div>
      <div class="sub3">${d.parts.rows.map((p) => `<div class="card part${p.ON_HAND > 0 ? "" : " out"}"><div class="pn">${esc(p.PART_NO)}</div>
        <div class="st"><span style="color:${p.ON_HAND > 0 ? "#22c55e" : "#ef4444"}">${p.ON_HAND > 0 ? num(p.ON_HAND) + " in stock" : "OUT OF STOCK"}</span> · lead ${num(p.LEAD_TIME_DAYS)} d</div></div>`).join("")}</div>
      <div class="method">all computed in ${esc(S.backend)}: risk by the OEE.FAILURE_RISK UDF · likely mode by OEE.V_MODE_LIKELIHOOD (root cause: five stream patterns vs learned fault signatures, step 2) · signals by OEE.V_SIGNAL_DEVIATIONS · parts and stage by OEE.V_MACHINE_STATUS · notes from OEE.MAINT_NOTES</div>
    </div></div>`;
    $("woraise")?.addEventListener("click", (e) => raiseWO(S.motor, e.currentTarget));
    $("woclose")?.addEventListener("click", () => closeWO(Math.round(wo.WO_ID)));
    $("golab").addEventListener("click", () => $("labsect").scrollIntoView());
    if (!S.histMode) loadHistory(top.mode);
  } catch (e) {
    el.innerHTML = `<span class="err">${esc(e.message)}</span>`;
  }
}

/* ============ OEE line impact ============ */
async function loadOEE() {
  const res = await api("/api/oee/losses?level=line");
  S.lines = res.rows;
  sqlNote("oee", res);
  renderOEE();
}
function renderOEE() {
  if (!S.lines.length) return;
  const rows = [...S.lines].sort((a, b) => a.OEE - b.OEE);
  const worst = rows[0].UNIT_ID;
  $("oee").innerHTML = rows.map((L) => {
    const ms = S.fleet.filter((m) => m.line_id === L.UNIT_ID);
    const atRisk = ms.filter((m) => m.status !== "healthy").sort((a, b) => b.risk - a.risk);
    const expected = ms.reduce((a, m) => a + (m.expected_down_hours || 0), 0); // from OEE.V_MACHINE_STATUS
    const daily = Math.max(ms.length, 1) * SHIFT_HOURS_PER_DAY;
    const aRisk = Math.max(0, L.AVAILABILITY - expected / daily);
    const oeeRisk = aRisk * L.PERFORMANCE * L.QUALITY;
    const segs = [["PRODUCTIVE_MIN", "productive", "Productive"], ["AVAILABILITY_LOSS_MIN", "avail", "Availability loss"], ["PERFORMANCE_LOSS_MIN", "perf", "Performance loss"], ["QUALITY_LOSS_MIN", "qual", "Quality loss"]]
      .map(([k, c, n]) => `<div class="seg ${c}" style="flex:${Math.max(L[k] / L.PLANNED_MIN, 0.003)}" title="${n}: ${pct(L[k] / L.PLANNED_MIN, 1)} of planned time"></div>`).join("");
    const lead = atRisk[0];
    const impact = lead
      ? `<div class="impact"><div class="k">At risk next 24 h</div>
        <div class="row"><b>${esc(lead.machine_id)}</b> ${esc(MODE_NAME[lead.likely_modes[0].mode].toLowerCase())} ${pct(lead.risk)}${atRisk.length > 1 ? ` <span class="muted">+${atRisk.length - 1} more</span>` : ""}</div>
        <div class="row">expected unplanned downtime <b class="red">${num(expected, 1)} h</b></div>
        <div class="row">OEE tomorrow if not handled ${pct(L.OEE, 1)} → <b class="red">${pct(oeeRisk, 1)}</b></div>
        <div class="row">handled in a planned window: <b class="green">holds ≈ ${pct(L.OEE, 1)}</b></div></div>`
      : `<div class="impact"><div class="row muted">No machine on this line above 10% risk.</div></div>`;
    return `<button type="button" class="card oeecard" data-line="${esc(L.UNIT_ID)}">
      <div class="hd"><span>${esc(lineLabel(L.UNIT_ID).toUpperCase())}</span><span class="o${L.UNIT_ID === worst ? " red" : ""}">OEE ${pct(L.OEE, 1)}</span></div>
      <div class="apq">A ${pct(L.AVAILABILITY, 1)} × P ${pct(L.PERFORMANCE, 1)} × Q ${pct(L.QUALITY, 1)}</div>
      <div class="lossbar" aria-hidden="true">${segs}</div>${impact}</button>`;
  }).join("");
  $("oee").querySelectorAll(".oeecard").forEach((b) => b.addEventListener("click", () => {
    const ms = S.fleet.filter((m) => m.line_id === b.dataset.line).sort((a, c) => c.risk - a.risk);
    if (ms[0]) { selectMachine(ms[0].machine_id); $("main").scrollTo({ top: 0 }); }
  }));
}

/* ============ what-if lab ============ */
let labTimer = null;
function setupLab() {
  const latest = S.series[S.series.length - 1];
  const m = S.fleet.find((x) => x.machine_id === S.motor);
  S.labVals = {}; S.labBase = null;
  $("sliders").innerHTML = LAB.map((c) => {
    const base = m?.signals[c.key]?.baseline;
    return `<div class="slider"><label for="lab-${c.key}"><span>${esc(c.label)}</span><b id="lab-${c.key}-v">${num(latest[c.key], c.d)} ${c.unit}</b></label>
      <input type="range" id="lab-${c.key}" min="${c.min}" max="${c.max}" step="${c.step}" value="${latest[c.key]}">
      <div class="base">latest ${num(latest[c.key], c.d)} · ${esc(TYPES[m?.machine_type] || "")} normal ${num(base, c.d)}</div></div>`;
  }).join("");
  LAB.forEach((c) => {
    const inp = $("lab-" + c.key);
    inp.addEventListener("input", () => setLab(c.key, parseFloat(inp.value)));
  });
  const presets = [
    ["Replace tool now", { TOOL_WEAR_MIN: 0 }],
    ["Fix bearing", { VIBRATION_MM_S: m?.signals.VIBRATION_MM_S?.baseline }],
    ["Restore cooling", { TEMP_DIFF_K: m?.signals.TEMP_DIFF_K?.baseline }],
    ["Ease load to nominal", { TORQUE_RATIO: 1.0 }],
  ];
  $("presets").innerHTML = presets.map((p, i) => `<button type="button" class="qbtn" data-p="${i}">${esc(p[0])}</button>`).join("")
    + `<button type="button" class="qbtn" data-p="reset">Reset to latest reading</button><span class="muted" style="align-self:center;font-size:12px">${esc(S.motor)}</span>`;
  $("presets").querySelectorAll("button").forEach((b) => b.addEventListener("click", () => {
    if (b.dataset.p === "reset") { setupLab(); return; }
    for (const [k, v] of Object.entries(presets[+b.dataset.p][1])) if (v != null) setLab(k, v, true);
  }));
  scoreLab();
}
function setLab(key, v, moveSlider = false) {
  const c = LAB.find((q) => q.key === key);
  v = Math.min(c.max, Math.max(c.min, v));
  S.labVals[key] = v;
  if (moveSlider) $("lab-" + key).value = v;
  $(`lab-${key}-v`).textContent = `${num(v, c.d)} ${c.unit}`;
  clearTimeout(labTimer); labTimer = setTimeout(scoreLab, 200);
}
async function scoreLab() {
  try {
    const res = await api("/api/whatif", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ machine_id: S.motor, overrides: S.labVals }) });
    sqlNote("lab", res);
    const r = res.rows[0].RISK_24H;
    if (S.labBase == null) S.labBase = r;
    const big = $("labrisk");
    big.textContent = pct(r);
    big.className = "labbig mono " + (r >= CRITICAL ? "red" : r >= WATCH ? "amber" : "green");
    $("labnow").style.width = (r * 100).toFixed(1) + "%";
    $("labnow").style.background = r >= CRITICAL ? "#ef4444" : r >= WATCH ? "#f59e0b" : "#22c55e";
    $("labbase").style.left = `calc(${(S.labBase * 100).toFixed(1)}% - 1px)`;
    const dlt = (r - S.labBase) * 100;
    $("labdelta").innerHTML = Object.keys(S.labVals).length
      ? `latest reading ${pct(S.labBase)} → <b style="color:${dlt <= 0 ? "#22c55e" : "#ef4444"}">${pct(r)}</b> (${dlt <= 0 ? "−" : "+"}${Math.abs(dlt).toFixed(0)} pts) · ${num(res.ms)} ms`
      : `latest reading, scored live · ${num(res.ms)} ms · grey tick marks the latest reading`;
  } catch (e) {
    $("labdelta").innerHTML = `<span class="err">${esc(e.message)}</span>`;
  }
}

/* ============ repair history ============ */
function renderHistButtons() {
  $("histbtns").innerHTML = MODES.map((md) => `<button type="button" class="qbtn${md === S.histMode ? " cur" : ""}" data-m="${md}" style="${md === S.histMode ? `border-color:${MODE_COLOR[md]}` : ""}">${esc(MODE_NAME[md])}</button>`).join("");
  $("histbtns").querySelectorAll("button").forEach((b) => b.addEventListener("click", () => loadHistory(b.dataset.m)));
}
async function loadHistory(mode) {
  S.histMode = mode; renderHistButtons();
  $("history").innerHTML = `<span class="muted">Loading…</span>`;
  try {
    const res = await api("/api/history/" + mode);
    sqlNote("hist", res);
    const rows = res.rows;
    if (!rows.length) { $("history").innerHTML = `<span class="muted">No unplanned ${esc(MODE_NAME[mode].toLowerCase())} stops recorded.</span>`; return; }
    const avg = rows.reduce((a, r) => a + (r.STOP_HOURS || 0), 0) / rows.length;
    const col = MODE_COLOR[mode];
    $("history").innerHTML = `<div class="histsum">${rows.length} most recent repairs · avg downtime ${num(avg, 1)} h · source: OEE.MAINT_NOTES joined to OEE.DOWNTIME</div><div class="hist">`
      + rows.map((r) => `<div class="card repair" style="border-left:3px solid ${col}">
        <div class="hd"><span><b>STOP #${Math.round(r.STOP_ID)}</b> · ${esc(r.MACHINE_ID)} · ${esc(TYPES[r.MACHINE_TYPE])}</span><span class="muted">${esc(dShort(ts(r.START_TS)))}</span></div>
        <div class="tx">${esc(r.NOTE_TEXT)}</div>
        <div class="ft">part: ${esc(r.PART_REPLACED || "—")} · downtime ${num(r.STOP_HOURS, 1)} h · ${esc(PLANTS[r.PLANT_ID])}</div></div>`).join("") + `</div>`;
  } catch (e) {
    $("history").innerHTML = `<span class="err">${esc(e.message)}</span>`;
  }
}

/* ============ fleet-scale benchmark ============ */
const STAGE_INFO = {
  "load": "IMPORT of the raw one-minute sensor CSV",
  "sensor fusion": "6-hour rolling windows across all sensors, per machine",
  "pattern detection": "every reading scored by OEE.FAILURE_RISK_BATCH, a numpy UDF run in parallel across all CPU cores",
  "alerts": "latest risk per machine, machines over threshold",
  "validation": "how often an alert was followed by a real stop within 24 h",
};
async function loadBench() {
  const el = $("bench");
  try {
    const res = await api("/api/bench");
    const rows = res.rows || [];
    sqlNote("bench", res.sql ? res : []);
    if (!rows.length) {
      el.innerHTML = `<div class="muted">No fleet-scale run yet. Run <code>python -m oee.cli bench</code> to generate about 14 million one-minute sensor readings (about 1 GB), fuse and score them inside ${esc(S.backend)}, and time every stage.</div>`;
      return;
    }
    const r0 = rows[0];
    const det = (r) => { try { return JSON.parse(r.DETAIL || "{}"); } catch { return {}; } };
    const total = rows.reduce((a, r) => a + r.SECONDS, 0);
    const maxS = Math.max(...rows.map((r) => r.SECONDS));
    const load = rows.find((r) => r.STAGE === "load");
    const val = rows.find((r) => r.STAGE === "validation");
    const vd = val ? det(val) : {};
    const ld = load ? det(load) : {};
    const detailText = (r) => {
      const d = det(r);
      if (r.STAGE === "load") return `${num(d.gb, 2)} GB`;
      if (r.STAGE === "alerts") return `${num(d.critical)} critical of ${num(d.machines)}`;
      if (r.STAGE === "validation") return `precision ${pct(d.alert_precision)}`;
      return `${big(r.ROWS_PER_S)} rows/s`;
    };
    el.innerHTML = `<div class="head">
        <div class="what"><b>${big(load ? load.ROWS_IN : r0.ROWS_IN)}</b> one-minute sensor readings · <b>${num(ld.gb, 2)} GB</b> · <b>${num(r0.MACHINES)}</b> machines × <b>${num(r0.DAYS)}</b> days<br>
          7 signals per reading, fused into ${FEATURE_COUNT} features and scored in-database · run ${esc(dTime(ts(r0.RUN_AT)))}</div>
        <div class="total"><div class="big">${num(total, 1)} s</div><div class="cap">end to end inside ${esc(S.backend)}</div></div>
      </div>`
      + rows.map((r) => `<div class="bstage">
        <span class="n">${Math.round(r.STAGE_ORDER)}</span>
        <span class="nm">${esc(r.STAGE.toUpperCase())}<small>${esc(STAGE_INFO[r.STAGE] || "")}</small></span>
        <span class="track"><i style="width:${Math.max(2, (r.SECONDS / maxS) * 100).toFixed(1)}%"></i></span>
        <span class="s">${num(r.SECONDS, 2)} s</span>
        <span class="r">${esc(detailText(r))}</span></div>`).join("")
      + (val && vd.alert_precision != null ? `<div class="verdict2">Of every reading the model flagged at 50%+ risk, <b>${pct(vd.alert_precision)}</b> were followed by a real unplanned stop within 24 hours, against a base rate of <b>${pct(vd.base_rate, 1)}</b>: <b>${num(vd.lift, 1)}×</b> better than chance.</div>` : "");
  } catch (e) {
    el.innerHTML = `<span class="err">${esc(e.message)}</span>`;
  }
}
const FEATURE_COUNT = 18;

/* ============ the five track steps ============ */
const STORE = { PNQ: "Pune", BLR: "Bengaluru", CHN: "Chennai", HYD: "Hyderabad WH" };
const STREAM_LABEL = { VIBRATION: "Vibration", TEMPERATURE: "Cooling gap", TORQUE: "Torque", POWER: "Power", TOOL: "Tool wear" };
const STREAMS = ["VIBRATION", "TEMPERATURE", "TORQUE", "POWER", "TOOL"];
const hrs = (h) => (h == null ? "—" : h === 0 ? "now" : h >= 48 ? num(h / 24, 0) + " d" : num(h, 0) + " h");
const inr = (v) => (v == null ? "—" : "₹" + num(v, 0));
const tag = (t, label) => `<span class="tag ${esc(t || "NONE")}">${esc(label || t || "—")}</span>`;
const heatColor = (z) => {
  const a = Math.max(0, Math.min(1, (z || 0) / 4));
  return z >= 0 ? `rgba(239,68,68,${(0.08 + a * 0.75).toFixed(2)})` : "rgba(59,158,255,.10)";
};

async function loadPillars() {
  const el = $("pillars");
  try {
    const res = await api("/api/pillars");
    sqlNote("pillars", res.sql ? res : []);
    const rows = res.rows || [];
    if (!rows.length) { el.innerHTML = `<div class="muted">Run <code>python -m oee.cli pillars</code> to measure compression, self-tuning and concurrency on this database.</div>`; return; }
    const get = (m) => rows.find((r) => r.METRIC === m);
    const conc = rows.filter((r) => r.PILLAR === "high concurrency").map((r) => ({ ...JSON.parse(r.DETAIL || "{}") }));
    const comp = get("raw / in-memory size, all OEE tables"), idx = get("indexes created automatically");
    const udf = get("Python UDFs in the OEE schema"), scored = get("rows scored in-database by model UDFs"), mpp = get("pattern detection rows/s (bench)");
    el.innerHTML = `<div class="minihead" style="margin-top:0">Exasol's five pillars · measured on this database</div><div class="kpis">
      <div class="kpi"><div class="k">In-memory MPP</div><div class="v b">${mpp ? big(mpp.VALUE_NUM) + "/s" : "—"}</div><div class="c">readings scored per second by the parallel batch UDF (bench)</div></div>
      <div class="kpi"><div class="k">Columnar compression</div><div class="v g">${comp && comp.VALUE_NUM ? num(comp.VALUE_NUM, 1) + "×" : "—"}</div><div class="c">${num(get("raw MB")?.VALUE_NUM, 0)} MB raw held in ${num(get("in-memory MB")?.VALUE_NUM, 0)} MB</div></div>
      <div class="kpi"><div class="k">Self-tuning</div><div class="v g">${idx && idx.VALUE_NUM != null ? num(idx.VALUE_NUM) : "—"}</div><div class="c">indexes Exasol built on its own · we wrote 0</div></div>
      <div class="kpi"><div class="k">High concurrency</div><div class="v">${conc.length ? num(conc[conc.length - 1].qps, 0) + " q/s" : "—"}</div><div class="c">${conc.map((c) => `${c.sessions} sess: ${num(c.qps, 0)} q/s, p95 ${num(c.p95_ms, 0)} ms`).join(" · ")}</div></div>
      <div class="kpi"><div class="k">In-database AI</div><div class="v">${udf ? num(udf.VALUE_NUM) : "—"} UDFs</div><div class="c">${scored ? big(scored.VALUE_NUM) : "—"} rows scored without leaving Exasol</div></div>
    </div>`;
  } catch (e) { el.innerHTML = `<span class="err">${esc(e.message)}</span>`; }
}

async function loadFusion() {
  const el = $("fusion");
  try {
    const d = await api("/api/fusion");
    sqlNote("fusion", [d.health, d.impact]);
    const h = d.health.rows, [naive, fused] = d.impact.rows;
    const rawTotal = h.reduce((a, r) => a + r.RAW_ROWS, 0);
    const avgComplete = h.reduce((a, r) => a + r.COMPLETE_SHARE, 0) / h.length;
    el.innerHTML = `<div class="lede">Every machine's sensors live in <b>five different systems</b>, each with its own machine ID, clock, units and timing.
      Exasol maps the IDs, converts local time to UTC and °C to K, spreads 15-minute energy readings over the minutes they cover,
      ignores meter data that has not arrived yet, and fills short gaps (every value is flagged measured, filled or missing), giving one clean minute-by-minute table.</div>
      <div class="kpis">
        <div class="kpi"><div class="k">Raw source rows</div><div class="v b">${big(rawTotal)}</div><div class="c">5 systems · 5 ID schemes · 2 time zones</div></div>
        <div class="kpi"><div class="k">Grid complete</div><div class="v g">${pct(avgComplete, 1)}</div><div class="c">measured or gap-filled, all streams</div></div>
        <div class="kpi"><div class="k">Alert precision</div><div class="v g">${pct(fused.ALERT_PRECISION)}</div><div class="c">combined data · vs ${pct(naive.ALERT_PRECISION)} raw-joined</div></div>
        <div class="kpi"><div class="k">Hours on alert</div><div class="v g">${pct(fused.ALERT_HOURS / fused.HOURS_SCORED, 1)}</div><div class="c">combined data · raw-joined alarms ${pct(naive.ALERT_HOURS / naive.HOURS_SCORED)} of the time</div></div>
      </div>
      <div class="split"><div class="tblwrap"><table class="tbl"><thead><tr><th>Source system</th><th>Stream</th><th>As delivered</th><th class="n">Rows</th><th>Measured · filled · missing</th></tr></thead><tbody>
        ${h.map((r) => `<tr><td>${esc(r.SOURCE_NAME)}</td><td>${esc(r.STREAM)}</td><td class="muted">${esc(r.SOURCE_FORMAT)}${r.AVG_DELAY_MIN ? ` · avg ${num(r.AVG_DELAY_MIN / 60, 1)} h late` : ""}</td><td class="n">${big(r.RAW_ROWS)}</td>
          <td><div class="bar2" title="${pct(r.MEASURED_SHARE, 1)} measured, ${pct(r.COMPLETE_SHARE - r.MEASURED_SHARE, 1)} filled"><i class="m" style="width:${(r.MEASURED_SHARE * 100).toFixed(1)}%"></i><i class="f" style="width:${((r.COMPLETE_SHARE - r.MEASURED_SHARE) * 100).toFixed(1)}%"></i></div></td></tr>`).join("")}
      </tbody></table></div>
      <div class="tblwrap"><table class="tbl"><thead><tr><th>Same model, scoring…</th><th class="n">Hours scored</th><th class="n">Stops warned</th><th class="n">Alert hours</th><th class="n">Precision</th></tr></thead><tbody>
        ${d.impact.rows.map((r) => `<tr class="${r.ORD === 2 ? "best" : ""}"><td>${esc(r.PIPELINE)}</td><td class="n">${num(r.HOURS_SCORED)}</td><td class="n">${num(r.STOPS_WARNED)} / ${num(r.STOPS)}</td><td class="n">${num(r.ALERT_HOURS)}</td><td class="n">${pct(r.ALERT_PRECISION)}</td></tr>`).join("")}
      </tbody></table>
      <div class="note">Held-out period only (after the model's training cutoff). "Raw-joined" takes each source at its reported timestamp, with no time-zone, unit or delay handling: what a quick join would give.</div></div></div>`;
  } catch (e) { el.innerHTML = `<span class="err">${esc(e.message)}</span>`; }
}

async function loadRootCause() {
  const el = $("rootcause");
  try {
    const d = await api("/api/rootcause/" + encodeURIComponent(S.motor));
    sqlNote("rootcause", [d.patterns, d.causes, d.signatures, d.accuracy]);
    const pats = d.patterns.rows, causes = d.causes.rows, acc = d.accuracy.rows[0] || {};
    const top = causes[0];
    const now = Object.fromEntries(pats.map((p) => [p.STREAM, p.BAD_Z]));
    const sig = {};
    d.signatures.rows.forEach((r) => { (sig[r.FAILURE_MODE] = sig[r.FAILURE_MODE] || {})[r.STREAM] = r.SIGNATURE_Z; });
    const active = pats.filter((p) => p.ACTIVE).map((p) => p.PATTERN);
    const order = causes.map((c) => c.FAILURE_MODE);
    el.innerHTML = `<div class="lede">For every machine-hour Exasol runs <b>one detector per sensor stream</b>, each against its machine type's normal.
      No single stream names the fault: overstrain and a drive fault both push power up, but only overstrain lifts torque too.
      The <b>combination</b> is matched to fault signatures learned from past failures (training period only).</div>
      <div class="lights">${pats.map((p) => `<div class="light ${p.ACTIVE ? "on" : p.BAD_Z >= 0.75 ? "warm" : ""}"><div class="dot"></div>
        <div class="s">${esc(STREAM_LABEL[p.STREAM])}</div><div class="z">${sigma(p.BAD_Z)}</div><div class="p">${esc(p.PATTERN)}</div></div>`).join("")}</div>
      <div class="split"><div>
        <div class="minihead">${esc(S.motor)} now vs learned fault signatures (σ in the harmful direction)</div>
        <div class="heat"><div></div>${STREAMS.map((k) => `<div class="h">${esc(STREAM_LABEL[k])}</div>`).join("")}
          <div class="rl now">● ${esc(S.motor)}</div>${STREAMS.map((k) => `<div class="c" style="background:${heatColor(now[k])}">${num(now[k], 1)}</div>`).join("")}
          ${order.map((mo, i) => `<div class="rl"><i style="width:8px;height:8px;border-radius:50%;background:${MODE_COLOR[mo]};display:inline-block"></i>${esc(MODE_NAME[mo])}</div>
            ${STREAMS.map((k) => `<div class="c" style="background:${heatColor(sig[mo]?.[k])}${i === 0 ? ";outline:1.5px solid var(--green)" : ""}">${num(sig[mo]?.[k], 1)}</div>`).join("")}`).join("")}
        </div></div>
      <div>
        <div class="minihead">Root cause · ${esc(S.motor)}</div>
        ${causes.slice(0, 3).map((c) => `<div class="modebar"><span>${esc(MODE_NAME[c.FAILURE_MODE])}</span><b>${pct(c.CONFIDENCE)}</b><div class="track"><i style="width:${(c.CONFIDENCE * 100).toFixed(1)}%;background:${MODE_COLOR[c.FAILURE_MODE]}"></i></div></div>`).join("")}
        <div class="finding" style="margin-top:10px">${active.length ? `Active patterns: <b>${esc(active.join(" + "))}</b>. ` : "No stream is past its 1.5σ alarm level. "}Closest signature: <b>${esc(MODE_NAME[top.FAILURE_MODE].toLowerCase())}</b>, which needs <b>${esc((S.fleet.find((m) => m.machine_id === S.motor) || {}).part?.part_no || "")}</b>.</div>
        <div class="kpis" style="grid-template-columns:repeat(3,minmax(0,1fr))">
          <div class="kpi"><div class="k">Held-out accuracy</div><div class="v g">${pct(acc.ACCURACY)}</div><div class="c">${num(acc.CORRECT)} of ${num(acc.STOPS)} stops · guessing the commonest mode: ${pct(acc.MAJORITY_BASELINE)}</div></div>
          <div class="kpi"><div class="k">Top-2</div><div class="v">${pct(acc.TOP2_ACCURACY)}</div><div class="c">true cause in the first two</div></div>
          <div class="kpi"><div class="k">Named early</div><div class="v b">${num(acc.AVG_HOURS_EARLY, 0)} h</div><div class="c">avg. first correct call before the stop</div></div>
        </div></div></div>`;
  } catch (e) { el.innerHTML = `<span class="err">${esc(e.message)}</span>`; }
}

async function loadSourcing() {
  const el = $("sourcing");
  try {
    const [d, fleet] = await Promise.all([api("/api/sourcing?machine=" + encodeURIComponent(S.motor)), api("/api/sourcing")]);
    sqlNote("sourcing", [d.plan, d.options, d.impact, d.reservations]);
    const away = {};
    fleet.plan.rows.filter((r) => r.HEALTH !== "healthy" && r.OPEN_WO_ID == null).forEach((r) => {
      const m = (away[r.MACHINE_ID] = away[r.MACHINE_ID] || { id: r.MACHINE_ID, local: true, ready: 0, mode: r.TOP_MODE });
      if (r.SOURCE_KIND !== "LOCAL") m.local = false;
      m.ready = Math.max(m.ready, r.ETA_HOURS);
    });
    const picks = Object.values(away).filter((m) => !m.local);
    const plan = d.plan.rows, opts = d.options.rows, imp = d.impact.rows[0] || {};
    const m0 = plan[0] || {};
    const kitReady = Math.max(...plan.map((p) => p.ETA_HOURS)), localOnly = Math.max(...plan.map((p) => p.LOCAL_ONLY_HOURS));
    const byPart = {};
    opts.forEach((o) => { (byPart[o.PART_NO] = byPart[o.PART_NO] || []).push(o); });
    const myRes = d.reservations.rows.filter((r) => r.MACHINE_ID === S.motor);
    el.innerHTML = `<div class="lede">The repair for <b>${esc(MODE_NAME[m0.TOP_MODE] || "—")}</b> on ${esc(S.motor)} needs this kit at <b>${esc(STORE[m0.PLANT_ID] || "")}</b>.
      Exasol checks the plant's own store, then <b>every other store</b> (free stock only: parts already reserved for open work orders are not offered twice), and compares each road transfer with the supplier's lead time.</div>
      <div class="kpis">
        <div class="kpi"><div class="k">Kit on site in</div><div class="v ${kitReady === 0 ? "g" : kitReady < 48 ? "a" : "r"}">${hrs(kitReady)}</div><div class="c">fastest source per part</div></div>
        <div class="kpi"><div class="k">Plant store or supplier only</div><div class="v r">${hrs(localOnly)}</div><div class="c">without scanning the network</div></div>
        <div class="kpi"><div class="k">Fleet: kits complete on site</div><div class="v">${num(imp.KITS_ON_SITE)} / ${num(imp.MACHINES_AT_RISK)}</div><div class="c">machines at watch or critical</div></div>
        <div class="kpi"><div class="k">Fleet: parts wait</div><div class="v g">${num(imp.WAIT_HOURS_NETWORK)} h</div><div class="c">vs ${num(imp.WAIT_HOURS_LOCAL_ONLY)} h from the plant store or supplier only</div></div>
      </div>
      ${picks.length ? `<div class="actions" style="margin:0 0 10px"><span class="msg">At-risk machines whose kit is not in their own plant store:</span>${picks.map((m) => `<button type="button" class="qbtn${m.id === S.motor ? " cur" : ""}" data-pick="${esc(m.id)}">${esc(m.id)} · ${esc(MODE_NAME[m.mode].toLowerCase())} · kit in ${hrs(m.ready)}</button>`).join("")}</div>` : ""}
      <div class="tblwrap"><table class="tbl"><thead><tr><th>Part</th><th class="n">Qty</th><th>Source</th><th>From</th><th class="n">Free there</th><th class="n">On site in</th><th class="n">Cost</th></tr></thead><tbody>
        ${Object.entries(byPart).map(([part, os]) => os.map((o, i) => `<tr class="${o.OPTION_RANK === 1 ? "best" : "dim"}"><td>${i === 0 ? `<b class="mono">${esc(part)}</b>` : ""}</td><td class="n">${i === 0 ? num(o.QTY) : ""}</td>
          <td>${tag(o.SOURCE_KIND)}${o.OPTION_RANK === 1 ? " ✓" : ""}</td><td>${esc(o.FROM_LOCATION ? STORE[o.FROM_LOCATION] : "supplier")}</td><td class="n">${o.AVAILABLE == null ? "—" : num(o.AVAILABLE)}</td>
          <td class="n">${hrs(o.ETA_HOURS)}</td><td class="n">${o.SOURCE_KIND === "LOCAL" ? "—" : inr(o.COST_INR)}</td></tr>`).join("")).join("")}
      </tbody></table></div>
      <div class="note">${myRes.length ? `Reserved for ${woName(myRes[0].WO_ID)}: ${myRes.map((r) => `${num(r.QTY)}× ${esc(r.PART_NO)} from ${esc(r.FROM_LOCATION ? STORE[r.FROM_LOCATION] : "supplier")}`).join(" · ")}. Released when the work order is closed.`
        : `Raising a work order (Diagnosis) reserves this kit at these sources in OEE.PART_RESERVATIONS.`} ${d.reservations.rows.length} reservation line${d.reservations.rows.length === 1 ? "" : "s"} open across the fleet.</div>`;
    el.querySelectorAll("[data-pick]").forEach((b) => b.addEventListener("click", async () => { await selectMachine(b.dataset.pick); $("s3").scrollIntoView(); }));
  } catch (e) { el.innerHTML = `<span class="err">${esc(e.message)}</span>`; }
}

async function loadQuality() {
  const el = $("quality");
  try {
    const d = await api("/api/quality");
    sqlNote("quality", [d.next_shift, d.impact]);
    const [model, rule] = d.impact.rows, nxt = d.next_shift.rows;
    const flagged = nxt.filter((r) => r.RECOMMENDATION !== "RUN");
    el.innerHTML = `<div class="lede">End-of-line inspection finds bad parts after the shift has made them. <b>OEE.QUALITY_RISK</b>, a second model compiled into an Exasol UDF,
      scores each machine's <b>next</b> shift from its condition now (tool wear, vibration, cooling, last shift's scrap), so a tool change or check can happen first.</div>
      <div class="kpis">
        <div class="kpi"><div class="k">New problems seen coming</div><div class="v g">${pct(model.NEW_PROBLEM_CATCH_RATE)}</div><div class="c">${num(model.NEW_PROBLEMS_FLAGGED)} of ${num(model.NEW_PROBLEMS)} first bad shifts · inspection: 0%</div></div>
        <div class="kpi"><div class="k">All bad shifts flagged</div><div class="v">${pct(model.CATCH_RATE)}</div><div class="c">vs ${pct(rule.CATCH_RATE)} with "last shift was bad"</div></div>
        <div class="kpi"><div class="k">Flag precision</div><div class="v">${pct(model.FLAG_PRECISION)}</div><div class="c">base rate ${pct(model.BASE_RATE)} · rule ${pct(rule.FLAG_PRECISION)}</div></div>
        <div class="kpi"><div class="k">Next shift</div><div class="v ${flagged.length ? "a" : "g"}">${num(flagged.length)}</div><div class="c">machines to HOLD or CHECK before start</div></div>
      </div>
      <div class="split"><div class="tblwrap"><table class="tbl"><thead><tr><th>Machine</th><th class="n">Scrap risk</th><th>Next shift</th><th class="n">Tool wear</th><th class="n">Last scrap</th></tr></thead><tbody>
        ${nxt.slice(0, 8).map((r) => `<tr><td class="mono">${esc(r.MACHINE_ID)}</td><td class="n">${pct(r.QUALITY_RISK)}</td><td>${tag(r.RECOMMENDATION)}</td><td class="n">${num(r.TOOL_WEAR_MIN)} min</td><td class="n">${pct(r.PREV_SCRAP_RATE, 1)}</td></tr>`).join("")}
      </tbody></table></div>
      <div class="tblwrap"><table class="tbl"><thead><tr><th>Held-out shifts</th><th class="n">Bad flagged</th><th class="n">New flagged</th><th class="n">Precision</th></tr></thead><tbody>
        ${d.impact.rows.map((r) => `<tr class="${r.ORD === 1 ? "best" : ""}"><td>${esc(r.METHOD_NAME)}</td><td class="n">${num(r.BAD_SHIFTS_FLAGGED)} / ${num(r.BAD_SHIFTS)}</td><td class="n">${num(r.NEW_PROBLEMS_FLAGGED)} / ${num(r.NEW_PROBLEMS)}</td><td class="n">${pct(r.FLAG_PRECISION)}</td></tr>`).join("")}
      </tbody></table>
      <div class="note">A bad shift scraps more than 7% of its output. A "new problem" is a bad shift after a good one, which inspection had nothing to warn about. Flag = risk ≥ 30% (CHECK); HOLD at ≥ 50%.</div></div></div>`;
  } catch (e) { el.innerHTML = `<span class="err">${esc(e.message)}</span>`; }
}

async function loadSuppliers() {
  const el = $("suppliers");
  try {
    const d = await api("/api/suppliers");
    sqlNote("suppliers", [d.accuracy, d.offers, d.bom]);
    const a = d.accuracy.rows[0] || {}, offers = d.offers.rows, bom = d.bom.rows;
    const byPart = {};
    offers.forEach((o) => { (byPart[o.PART_NO] = byPart[o.PART_NO] || []).push(o); });
    const total = bom.reduce((s, b) => s + (b.LINE_TOTAL_INR || 0), 0);
    el.innerHTML = `<div class="lede">Three suppliers, three formats: a <b>CSV</b> in rupees and days, <b>JSON</b> in dollars and weeks, and a CSV with prices like "Rs. 1,234.50" and delivery like "3 wks".
      None uses our part numbers. Exasol reads all three, normalises each description, and matches it on the manufacturer's code. Exact matches go straight into the order;
      near matches (a typo, or a different but similar part such as a 6204 bearing next to our 6205) go to a buyer for review.</div>
      <div class="kpis">
        <div class="kpi"><div class="k">Catalog lines read</div><div class="v b">${num(a.CATALOG_LINES)}</div><div class="c">${num(a.LINES_FOR_OUR_PARTS)} are our parts</div></div>
        <div class="kpi"><div class="k">Auto-matched</div><div class="v g">${num(a.AUTO_CORRECT)} / ${num(a.AUTO_MATCHED)}</div><div class="c">precision ${pct(a.AUTO_PRECISION)} · recall ${pct(a.AUTO_RECALL)}</div></div>
        <div class="kpi"><div class="k">Sent to review</div><div class="v a">${num(a.SENT_TO_REVIEW)}</div><div class="c">${num(a.REVIEW_CORRECT)} ${a.REVIEW_CORRECT === 1 ? "is a real match" : "are real matches"} · never ordered automatically</div></div>
        <div class="kpi"><div class="k">Order value</div><div class="v">${inr(total)}</div><div class="c">${num(bom.length)} BOM line${bom.length === 1 ? "" : "s"}</div></div>
      </div>
      <div class="minihead">BOM to order</div>
      <div class="tblwrap"><table class="tbl"><thead><tr><th>Part</th><th>Why</th><th class="n">Urgent</th><th class="n">Replenish</th><th class="n">Order</th><th>Supplier · their code · their description</th><th class="n">Unit</th><th class="n">Lead</th><th class="n">Line</th></tr></thead><tbody>
        ${bom.length ? bom.map((b) => `<tr><td class="mono"><b>${esc(b.PART_NO)}</b></td><td>${tag(b.PRIORITY === "NO VERIFIED SUPPLIER" ? "HOLD" : b.PRIORITY, b.PRIORITY)}</td><td class="n">${num(b.URGENT_QTY)}</td><td class="n">${num(b.REPLENISH_QTY)}</td><td class="n">${num(b.ORDER_QTY)}</td>
          <td>${esc(b.SUPPLIER_NAME || "—")} · <span class="mono">${esc(b.SUPPLIER_CODE || "")}</span> · <span class="muted">${esc(b.SUPPLIER_DESCRIPTION || "")}</span></td><td class="n">${inr(b.UNIT_PRICE_INR)}</td><td class="n">${num(b.LEAD_DAYS)} d</td><td class="n">${inr(b.LINE_TOTAL_INR)}</td></tr>`).join("")
          : `<tr><td colspan="9" class="muted">Nothing to order: the network covers every at-risk repair and stays above reorder points.</td></tr>`}
      </tbody></table></div>
      <div class="minihead">The same part, three ways · how each supplier wrote it</div>
      <div class="tblwrap"><table class="tbl"><thead><tr><th>Our part</th><th>Supplier</th><th>Their description</th><th>Match</th><th class="n">₹ / unit</th><th class="n">Lead</th></tr></thead><tbody>
        ${Object.entries(byPart).map(([part, os]) => os.map((o, i) => `<tr class="${o.MATCH_TYPE === "REVIEW" ? "dim" : ""}"><td class="mono">${i === 0 ? `<b>${esc(part)}</b>` : ""}</td><td>${esc(o.SUPPLIER_NAME)} <span class="muted">(${esc(o.CATALOG_FORMAT)})</span></td>
          <td class="mono">${esc(o.DESCRIPTION)}</td><td>${tag(o.MATCH_TYPE, o.MATCH_TYPE === "REVIEW" ? `REVIEW · ${o.EDIT_DIST} edit${o.EDIT_DIST === 1 ? "" : "s"}` : "EXACT")}</td><td class="n">${inr(o.UNIT_PRICE_INR)}</td><td class="n">${num(o.LEAD_DAYS)} d</td></tr>`).join("")).join("")}
      </tbody></table></div>`;
  } catch (e) { el.innerHTML = `<span class="err">${esc(e.message)}</span>`; }
}

/* ============ machine selection ============ */
async function selectMachine(id) {
  S.motor = id; S.winEnd = null; S.histMode = null;
  renderFloor(); renderDetails();
  try {
    const d = await api("/api/machine/" + encodeURIComponent(id));
    S.series = d.series.rows; S.stops = d.stops.rows;
    sqlNote("feed", [d.info, d.series, d.stops]);
    drawFeed(true);
    setupLab();
  } catch (e) {
    $("legend").innerHTML = `<span class="err">${esc(e.message)}</span>`;
  }
  loadDiagnosis(); loadRootCause(); loadSourcing();
}

/* ============ nav highlight ============ */
function watchSections() {
  const links = [...document.querySelectorAll(".nav a")].filter((a) => a.getAttribute("href").startsWith("#"));
  const main = $("main");
  const targets = links.map((a) => document.querySelector(a.getAttribute("href")));
  const update = () => {
    let cur = links[0];
    const top = main.getBoundingClientRect().top;
    targets.forEach((t, i) => { if (t && i > 0 && t.getBoundingClientRect().top - top < 140) cur = links[i]; });
    links.forEach((a) => a.classList.toggle("cur", a === cur));
  };
  main.addEventListener("scroll", update);
  links[0].addEventListener("click", (e) => { e.preventDefault(); main.scrollTo({ top: 0 }); });
}

let resizeT = null;
window.addEventListener("resize", () => { clearTimeout(resizeT); resizeT = setTimeout(() => drawFeed(false), 120); });

/* ============ boot ============ */
(async () => {
  renderTabs(); watchSections();
  if (!(await loadHealth())) return;
  try {
    await loadFleet();
    await loadOEE();
    loadBench(); loadPillars(); loadFusion(); loadQuality(); loadSuppliers();
    const first = [...S.fleet].sort((a, b) => b.risk - a.risk)[0];
    if (first) await selectMachine(first.machine_id);
  } catch (e) {
    $("main").insertAdjacentHTML("afterbegin", `<div class="card err">${esc(e.message)}</div>`);
  }
})();
