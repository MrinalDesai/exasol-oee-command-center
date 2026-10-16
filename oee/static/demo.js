/* Fault pattern search demo.
   1. pick a fault  2. its five-stream pattern (learned signature)
   3. Exasol searches the whole history for it (timed on the server)
   4. a held-out failure replays hour by hour until the same pattern selects the fault */
"use strict";

const FAULTS = [
  { mode: "BEARING", name: "Bearing", own: "VIB", color: "#facc15", desc: "Vibration ramps up; the other streams stay normal." },
  { mode: "HEAT_DISSIPATION", name: "Coolant", own: "TEMP", color: "#38bdf8", desc: "The process–air temperature gap collapses." },
  { mode: "OVERSTRAIN", name: "Overstrain", own: "TORQ", color: "#f59e0b", desc: "Torque climbs above nominal, and power climbs with it." },
  { mode: "POWER", name: "Drive", own: "PWR", color: "#a78bfa", desc: "Power drifts out of band while torque stays normal." },
];
const STREAMS = [
  { key: "VIB", sig: "VIBRATION", name: "Vibration", hint: "condition monitor" },
  { key: "TEMP", sig: "TEMPERATURE", name: "Cooling gap", hint: "PLC historian" },
  { key: "TORQ", sig: "TORQUE", name: "Torque", hint: "drive controller" },
  { key: "PWR", sig: "POWER", name: "Power", hint: "energy meter" },
  { key: "TOOL", sig: "TOOL", name: "Tool wear", hint: "production system" },
];
const MODE_NAME = { TOOL_WEAR: "Tool wear", HEAT_DISSIPATION: "Coolant", POWER: "Drive", OVERSTRAIN: "Overstrain", BEARING: "Bearing" };
const STORE = { PNQ: "Pune", BLR: "Bengaluru", CHN: "Chennai", HYD: "Hyderabad warehouse" };
const STEP_MS = 260;          // one hour of the replay per step

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const num = (v, d = 0) => (v == null || isNaN(v) ? "—" : Number(v).toLocaleString("en-IN", { minimumFractionDigits: d, maximumFractionDigits: d }));
const pct = (v) => (v == null || isNaN(v) ? "—" : Math.round(v * 100) + "%");
const big = (v) => (v == null ? "—" : v >= 1e6 ? (v / 1e6).toFixed(1) + " M" : v >= 1e3 ? (v / 1e3).toFixed(1) + " k" : num(v));
const ts = (s) => new Date(String(s).replace(" ", "T"));
const when = (d) => d.toLocaleDateString("en-GB", { day: "numeric", month: "short" }) + " " + d.toTimeString().slice(0, 5);

const S = { backend: "Exasol", fault: null, run: 0, timer: null, rule: { alarm_z: 2.5, min_hours: 3 } };

async function api(path) {
  const r = await fetch(path);
  let body = {};
  try { body = await r.json(); } catch { /* not JSON */ }
  if (!r.ok) throw new Error(typeof body.detail === "string" ? body.detail : `Request failed (${r.status})`);
  return body;
}

function sqlNote(key, list) {
  const el = document.querySelector(`.sqlnote[data-sql="${key}"]`);
  const qs = (Array.isArray(list) ? list : [list]).filter((q) => q && q.sql);
  if (!el) return;
  if (!qs.length) { el.innerHTML = ""; return; }
  const ms = qs.reduce((a, q) => a + (q.ms || 0), 0);
  el.innerHTML = `<details><summary><span class="db">⛁ ${esc(S.backend)}</span><span>${qs.length} ${qs.length === 1 ? "query" : "queries"} · ${num(ms)} ms</span><u>show SQL</u></summary><pre>${esc(qs.map((q) => q.sql.trim()).join(";\n\n") + ";")}</pre></details>`;
  const det = el.querySelector("details");
  det.addEventListener("toggle", () => { det.querySelector("u").textContent = (det.open ? "hide" : "show") + " SQL"; });
}

/* ---------- 1. buttons ---------- */
function renderButtons() {
  $("faults").innerHTML = FAULTS.map((f) => `<button type="button" class="fault" style="--c:${f.color}" data-mode="${f.mode}" aria-pressed="false">
      <span class="nm"><i style="background:${f.color}"></i>${esc(f.name)}</span><span class="ds">${esc(f.desc)}</span></button>`).join("");
  $("faults").querySelectorAll(".fault").forEach((b) => b.addEventListener("click", () => run(b.dataset.mode)));
}
const setBusy = (busy) => $("faults").querySelectorAll(".fault").forEach((b) => { b.disabled = busy; });

/* ---------- 2. signature ---------- */
function renderSignature(f, rows) {
  const max = Math.max(5, ...rows.map((r) => r.SIGNATURE_Z || 0));
  const x = (v) => Math.max(0, Math.min(1, (v || 0) / max)) * 100;
  const learned = rows.length ? rows[0].LEARNED_FROM_STOPS : null;
  $("sigbars").innerHTML = STREAMS.map((s) => {
    const r = rows.find((q) => q.STREAM === s.sig) || {};
    const own = s.key === f.own;
    return `<div class="sigrow${own ? " own" : ""}"><span>${esc(s.name)}</span>
      <div class="tr"><i style="width:${x(r.SIGNATURE_Z).toFixed(1)}%;background:${(r.SIGNATURE_Z || 0) >= 1.5 ? f.color : "#2a3a4e"}"></i><span class="al" style="left:${x(S.rule.alarm_z).toFixed(1)}%"></span></div>
      <b>${(r.SIGNATURE_Z >= 0 ? "+" : "") + num(r.SIGNATURE_Z, 1)}σ</b></div>`;
  }).join("") + `<div class="sigcap">Average deviation of each stream from its machine type's normal in the 12 hours before past <b>${esc(f.name.toLowerCase())}</b> failures${learned ? ` (learned from ${num(learned)} failures before the training cutoff)` : ""}. The grey tick is the ${S.rule.alarm_z}σ alarm level. No single stream identifies the fault; the combination does.</div>`;
}

/* ---------- 3. search ---------- */
function renderScanning(rows) {
  const t0 = performance.now();
  $("searchbody").innerHTML = `<div class="scanning" id="scanning">Scanning ${rows ? big(rows) + " readings" : "every reading"} in ${esc(S.backend)}</div>`;
  return t0;
}
function renderSearch(f, d) {
  const s = d.summary.rows[0] || {};
  const prec = s.EPISODES ? s.EPISODES_CAME_TRUE / s.EPISODES : null;
  const rec = s.FAILURES ? s.FAILURES_WARNED / s.FAILURES : null;
  const lift = prec != null && d.base_rate ? prec / d.base_rate : null;
  const secs = d.search.ms / 1000;
  $("searchbody").innerHTML = `<div class="bigtime"><span class="t">${secs < 10 ? num(secs, 2) : num(secs, 1)} s</span>
      <span class="w">to scan <b>${big(d.rows_scanned)}</b> readings from <b>${num(d.machines)}</b> machines<br>${esc(d.source)} · <b>${big(d.rows_scanned / Math.max(secs, 1e-3))}</b> readings/s</span></div>
    <div class="found">
      <div class="kpi"><div class="k">Pattern found</div><div class="v">${num(s.EPISODES)}×</div><div class="c">on ${num(s.MACHINES)} machines · held ${S.rule.min_hours} h+ at ${S.rule.alarm_z}σ+</div></div>
      <div class="kpi"><div class="k">Came true</div><div class="v g">${pct(prec)}</div><div class="c">followed by a ${esc(f.name.toLowerCase())} failure within ${d.rule.follow_hours} h${lift ? ` · ${num(lift, 0)}× chance (${pct(d.base_rate)})` : ""}</div></div>
      <div class="kpi"><div class="k">Failures warned</div><div class="v b">${num(s.FAILURES_WARNED)} / ${num(s.FAILURES)}</div><div class="c">${pct(rec)} of ${esc(f.name.toLowerCase())} failures had the pattern first</div></div>
    </div>
    <div class="eps">Latest matches: ${d.episodes.rows.map((e) => `<span class="${e.FAILED_AT ? "y" : "n"}">${esc(e.MACHINE_ID)} ${esc(when(ts(e.FIRST_TS)))}${e.FAILED_AT ? " → failed" : ""}</span>`).join("")}</div>`;
  sqlNote("search", [d.search, d.summary, d.episodes]);
}

/* ---------- 4. live stream replay ---------- */
function sizeCanvas(c) {
  const r = c.getBoundingClientRect(), dpr = window.devicePixelRatio || 1;
  c.width = Math.max(10, Math.round(r.width * dpr)); c.height = Math.max(10, Math.round(r.height * dpr));
  const g = c.getContext("2d"); g.setTransform(dpr, 0, 0, dpr, 0, 0);
  return { g, w: r.width, h: r.height };
}
function buildStrips(f) {
  $("strips").innerHTML = STREAMS.map((s) => `<div class="strip${s.key === f.own ? " own" : ""}" style="--c:${f.color}">
      <div class="lb"><b>${esc(s.name)}</b>${esc(s.hint)}</div><canvas id="cv_${s.key}" aria-label="${esc(s.name)} stream"></canvas><div class="val mono" id="vv_${s.key}">—</div></div>`).join("");
}
function selectionIndex(rows, f) {
  let runLen = 0;
  for (let i = 0; i < rows.length; i++) {
    const ok = rows[i].TOP_MODE === f.mode && (rows[i][f.own] || 0) >= S.rule.alarm_z;
    runLen = ok ? runLen + 1 : 0;
    if (runLen >= S.rule.min_hours) return { at: i, from: i - runLen + 1 };
  }
  return null;
}
function drawStrips(rows, upto, f, sel) {
  const n = rows.length;
  STREAMS.forEach((s) => {
    const { g, w, h } = sizeCanvas($("cv_" + s.key));
    const vals = rows.map((r) => r[s.key] || 0);
    const hi = Math.max(6, ...vals) + 0.5, lo = Math.min(-2, ...vals);
    const X = (i) => 4 + (i / Math.max(n - 1, 1)) * (w - 8);
    const Y = (v) => h - 4 - ((v - lo) / (hi - lo)) * (h - 8);
    g.clearRect(0, 0, w, h);
    if (sel && upto >= sel.at) {                       // the matched pattern, shaded
      g.fillStyle = f.color + "2e";
      g.fillRect(X(sel.from), 0, X(upto) - X(sel.from), h);
    }
    g.strokeStyle = "rgba(132,148,167,.55)"; g.setLineDash([4, 4]); g.lineWidth = 1;
    g.beginPath(); g.moveTo(0, Y(S.rule.alarm_z)); g.lineTo(w, Y(S.rule.alarm_z)); g.stroke(); g.setLineDash([]);
    g.strokeStyle = "rgba(132,148,167,.25)"; g.beginPath(); g.moveTo(0, Y(0)); g.lineTo(w, Y(0)); g.stroke();
    for (let i = 1; i <= upto && i < n; i++) {
      const hot = vals[i] >= S.rule.alarm_z;
      g.strokeStyle = hot ? "#ef4444" : (s.key === f.own ? f.color : "#5b7896");
      g.lineWidth = hot || s.key === f.own ? 2.2 : 1.6;
      g.beginPath(); g.moveTo(X(i - 1), Y(vals[i - 1])); g.lineTo(X(i), Y(vals[i])); g.stroke();
    }
    const v = vals[Math.min(upto, n - 1)];
    const el = $("vv_" + s.key);
    el.textContent = (v >= 0 ? "+" : "") + v.toFixed(1) + "σ";
    el.classList.toggle("hot", v >= S.rule.alarm_z);
  });
}
function kitText(kit) {
  return kit.map((k) => `<b>${esc(k.PART_NO)}</b> ×${num(k.QTY)} ${k.SOURCE_KIND === "LOCAL" ? "in the plant store" : k.SOURCE_KIND === "TRANSFER" ? `from ${esc(STORE[k.FROM_LOCATION] || k.FROM_LOCATION)} in ${num(k.ETA_HOURS)} h` : `from the supplier in ${num(k.ETA_HOURS / 24)} days`}`).join(" · ");
}
function playReplay(f, d, runId) {
  const p = d.pick.rows[0], rows = d.stream.rows, kit = d.kit.rows;
  const stop = ts(p.START_TS);
  const sel = selectionIndex(rows, f);
  $("livemachine").textContent = `· ${p.MACHINE_ID}`;
  $("selected").hidden = true;
  $("replay").hidden = true;
  buildStrips(f);
  let i = 0;
  clearInterval(S.timer);
  S.timer = setInterval(() => {
    if (runId !== S.run) { clearInterval(S.timer); return; }
    const r = rows[i];
    $("liveclock").textContent = `${when(ts(r.READING_TS))} · root cause now: ${MODE_NAME[r.TOP_MODE] || "—"} ${pct(r.TOP_CONFIDENCE)} · 24 h risk ${pct(r.RISK_24H)}`;
    drawStrips(rows, i, f, sel);
    if (sel && i === sel.at) {
      const hoursBefore = Math.round((stop - ts(r.READING_TS)) / 3.6e6);
      const b = $("selected");
      b.style.setProperty("--c", f.color);
      b.innerHTML = `<div class="h">FAULT SELECTED · ${esc(f.name.toUpperCase())} · ${esc(p.MACHINE_ID)}</div>
        The ${esc(f.name.toLowerCase())} pattern has held for ${S.rule.min_hours} hours. Root cause: <b>${esc(MODE_NAME[r.TOP_MODE])} ${pct(r.TOP_CONFIDENCE)}</b>.
        Repair kit: ${kit.map((k) => `<b>${esc(k.PART_NO)}</b> ×${num(k.QTY)}`).join(" · ")}. Searching the stores (step 5).`;
      b.hidden = false;
      b.dataset.hours = hoursBefore;
      findKit(f, p.PLANT_ID, p.MACHINE_ID, runId);
    }
    i += 1;
    if (i >= rows.length) {
      clearInterval(S.timer);
      const b = $("selected");
      $("livenote").innerHTML = `This machine actually stopped for <b>${esc(f.name.toLowerCase())}</b> at ${esc(when(stop))}${b.hidden ? "" : `, <b>${esc(b.dataset.hours)} hours</b> after the fault was selected`}.
        Replay of a held-out failure (after the model's training cutoff, so nothing here was learned from it): one of the ${esc(f.name.toLowerCase())} failures the root cause named correctly.`;
      $("replay").hidden = false;
      setBusy(false);
    }
  }, STEP_MS);
}


/* ---------- 5. find the repair kit ---------- */
const hrs = (h) => (h == null ? "—" : h === 0 ? "on site" : h >= 48 ? num(h / 24, 0) + " days" : num(h, 0) + " h");
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function findKit(f, plant, machine, runId) {
  const sect = $("kitsect");
  sect.hidden = false;
  $("kittitle").textContent = `${f.name.toLowerCase()} repair on ${machine}, ${STORE[plant] || plant}`;
  $("kitlocal").innerHTML = `<div class="scanning small">Searching the plant's own store system</div>`;
  $("kitnet").innerHTML = "";
  sect.scrollIntoView({ behavior: "smooth", block: "nearest" });
  let d;
  try { d = await api(`/api/demo/parts?mode=${encodeURIComponent(f.mode)}&plant=${encodeURIComponent(plant)}`); }
  catch (e) { $("kitlocal").innerHTML = `<span class="err">${esc(e.message)}</span>`; return; }
  if (runId !== S.run) return;
  sqlNote("kit", [d.local, d.network]);
  const local = d.local.rows;
  const missing = local.filter((r) => r.LOCAL_STATUS !== "FOUND");
  S.missing = Object.fromEntries(missing.map((r) => [r.PART_NO, r.NEEDED]));
  if (missing.length) { S.order = { part: missing[0].PART_NO, qty: missing[0].NEEDED }; renderOrderChips(); $("ordbody").innerHTML = ""; }
  $("kitlocal").innerHTML = `<div class="storedb"><div class="hd"><b>Database 1 · the plant's own store</b><span class="t">${num(d.local.ms, 0)} ms</span></div>
    <div class="sys">${esc(d.own_table)} · ${esc(d.own_system)}</div>
    <table class="netgrid" style="margin-top:8px"><thead><tr><th style="text-align:left">Part</th><th>Need</th><th>Free here</th><th></th></tr></thead><tbody>
    ${local.map((r) => `<tr><td>${esc(r.PART_NO)}</td><td>${num(r.NEEDED)}</td><td>${num(r.FREE_QTY)}</td><td><span class="st ${r.LOCAL_STATUS}">${r.LOCAL_STATUS}</span></td></tr>`).join("")}
    </tbody></table>
    <div class="kitsum">${missing.length ? `<b class="r">${missing.length} of ${local.length}</b> kit parts not available here.` : `The whole kit is here.`}</div></div>`;
  if (!missing.length) { $("kitnet").innerHTML = ""; return; }

  $("kitnet").innerHTML = `<div class="storedb"><div class="scanning small">Searching ${d.systems.length - 1} other store systems</div></div>`;
  await sleep(900);
  if (runId !== S.run) return;
  const net = d.network.rows;
  const cols = [d.systems.find((x) => x.location === plant), ...d.systems.filter((x) => x.location !== plant)];
  const parts = [...new Set(net.map((r) => r.PART_NO))];
  const at = (part, loc) => net.find((r) => r.PART_NO === part && r.LOCATION_ID === loc);
  const sup = (part) => net.find((r) => r.PART_NO === part && r.SOURCE_KIND === "SUPPLIER");
  const pick = (part) => net.find((r) => r.PART_NO === part && r.OPTION_RANK === 1);
  const cell = (r) => {
    if (!r) return `<span class="cell">—</span>`;
    const chosen = r.OPTION_RANK === 1;
    const has = r.SOURCE_KIND === "SUPPLIER" || (r.FREE_QTY || 0) > 0;
    const what = r.SOURCE_KIND === "SUPPLIER" ? "order" : `${num(r.FREE_QTY)} free`;
    return `<span class="cell${chosen ? " pick" : has ? " has" : ""}">${chosen ? "✓ " : ""}${what}<small>${hrs(r.ETA_HOURS)}${r.BIN_LOCATION ? " · bin " + esc(r.BIN_LOCATION) : ""}</small></span>`;
  };
  const kitReady = Math.max(...parts.map((p) => pick(p)?.ETA_HOURS ?? 0));
  const localOnly = Math.max(...local.map((r) => (r.LOCAL_STATUS === "FOUND" ? 0 : sup(r.PART_NO)?.ETA_HOURS ?? 0)));
  $("kitnet").innerHTML = `<div class="storedb"><div class="hd"><b>Every store system, read as one by Exasol</b><span class="t">${num(d.network.ms, 0)} ms</span></div>
    <div class="sys">${d.systems.map((x) => esc(x.table)).join(" · ")} · part numbers written ${[...new Set(d.systems.map((x) => x.part_format))].join(", ")}</div>
    <table class="netgrid" style="margin-top:8px"><thead><tr><th style="text-align:left">Part</th>
      ${cols.map((c) => `<th class="${c.location === plant ? "own" : ""}">${esc(STORE[c.location] || c.location)}<small>${esc(c.system.split(" (")[0])}</small></th>`).join("")}<th>Supplier<small>lead time</small></th></tr></thead><tbody>
    ${parts.map((p) => {
      const lr = local.find((r) => r.PART_NO === p);
      const best = pick(p);
      const wrong = lr && lr.LOCAL_STATUS !== "FOUND" && best && best.SOURCE_KIND === "TRANSFER";
      return `<tr><td>${esc(p)}${wrong ? `<br><span class="wrong">WRONG LOCATION · held at ${esc(STORE[best.LOCATION_ID] || best.LOCATION_ID)}</span>` : ""}</td>
        ${cols.map((c) => `<td>${cell(at(p, c.location))}</td>`).join("")}<td>${cell(sup(p))}</td></tr>`;
    }).join("")}
    </tbody></table>
    <div class="kitsum">Whole kit on site in <b class="g">${hrs(kitReady)}</b> by transferring from other stores, instead of <b class="r">${hrs(localOnly)}</b> waiting on the supplier for what the plant store lacks.</div></div>`;
}


/* ---------- 6. quality check before the shift ---------- */
const QC_WORD = { RUN: "Go", CHECK: "Check first", HOLD: "Stop, fix first" };

function qcReason(e) {
  const parts = [
    { z: e.TOOL_Z, text: `the tool is worn (${num(e.TOOL_WEAR_MIN)} min, normal about ${num(e.TOOL_MU)})` },
    { z: e.VIB_Z, text: "the machine is shaking more than normal" },
    { z: e.HEAT_Z, text: "it is running hot (cooling is weak)" },
  ].filter((p) => p.z != null).sort((a, b) => b.z - a.z);
  return parts.length ? parts[0].text : "its condition has drifted";
}

async function runQualityCheck() {
  const btn = $("qcrun");
  btn.disabled = true;
  const body = $("qcbody");
  const t0 = performance.now();
  body.innerHTML = `<div class="scanning small" id="qcscan">Checking every machine in ${esc(S.backend)}</div>`;
  let d;
  try { d = await api("/api/demo/quality"); }
  catch (e) { body.innerHTML = `<span class="err">${esc(e.message)}</span>`; btn.disabled = false; return; }
  sqlNote("qc", [d.score, d.next_shift, d.example, d.impact]);
  const ns = d.next_shift.rows;
  const counts = { RUN: 0, CHECK: 0, HOLD: 0 };
  ns.forEach((r) => { counts[r.RECOMMENDATION] = (counts[r.RECOMMENDATION] || 0) + 1; });
  body.innerHTML = `<div class="qctime fadein"><span class="t">${num(d.score.ms / 1000, 2)} s</span>
      <span class="w">to check <b>${num(d.shifts_scored)}</b> shifts with the quality model inside ${esc(S.backend)}</span></div>
    <div class="minihead">Next shift, every machine · <span style="color:var(--green)">${counts.RUN} go</span> · <span style="color:var(--amber)">${counts.CHECK} check first</span> · <span style="color:var(--red)">${counts.HOLD} stop</span></div>
    <div class="tiles" id="tiles">${ns.map((r) => `<div class="tile ${r.RECOMMENDATION}"><i></i><div><b>${esc(r.MACHINE_ID)}</b><span>${QC_WORD[r.RECOMMENDATION]}</span></div></div>`).join("")}</div>
    <div id="qcstory"></div>`;
  const tiles = [...document.querySelectorAll("#tiles .tile")];
  for (let i = 0; i < tiles.length; i++) { tiles[i].classList.add("in"); await sleep(45); }
  await sleep(600);

  const e = d.example.rows[0];
  const [model, rule] = d.impact.rows;
  const story = $("qcstory");
  if (!e) { story.innerHTML = ""; btn.disabled = false; return; }
  const start = ts(e.SHIFT_START);
  const avoided = Math.max(0, e.BAD_PARTS - e.NORMAL_SCRAP);
  story.innerHTML = `<div class="story fadein"><h3>Example from the past: ${esc(e.MACHINE_ID)}, shift of ${esc(when(start))}</h3>
      <div class="lane"><div class="lb">The old way<small>check at the end of the line</small></div>
        <div class="shiftbar" id="oldbar"><div class="fill" id="oldfill"></div><div class="hrs"><span>shift starts</span><span>4 h</span><span>8 h</span></div></div></div>
      <div class="verdict3" id="oldtext"></div>
      <div class="lane" id="newlane" style="visibility:hidden"><div class="lb">With ${esc(S.backend)}<small>check before the shift starts</small></div>
        <div class="shiftbar"><div class="flag start">STOP · fix it first</div><div class="hrs" style="justify-content:flex-end"><span>shift does not start until fixed</span></div></div></div>
      <div class="verdict3" id="newtext"></div>
      <div id="savedbox"></div></div>`;
  const fill = $("oldfill");
  for (let h = 0; h <= 40; h++) { fill.style.width = (h / 40 * 100).toFixed(1) + "%"; await sleep(55); }
  $("oldbar").insertAdjacentHTML("beforeend", `<div class="flag end">${num(e.BAD_PARTS)} bad parts found</div>`);
  $("oldtext").innerHTML = `The shift ran for 8 hours. The check at the end found <b>${num(e.BAD_PARTS)}</b> bad parts out of ${num(e.TOTAL_COUNT)}, already made. The shift before it had been fine, so nobody saw it coming.`;
  await sleep(1200);
  $("newlane").style.visibility = "visible";
  $("newlane").classList.add("fadein");
  $("newtext").innerHTML = `Before the shift started, the quality check said <b style="color:var(--red)">stop</b>: ${esc(qcReason(e))}.`;
  await sleep(900);
  $("savedbox").innerHTML = `<div class="saved fadein">About <b>${num(avoided)}</b> bad parts avoided by fixing it first (${num(e.BAD_PARTS)} found, about ${num(e.NORMAL_SCRAP)} is normal scrap).</div>
    <div class="bottomline fadein">
      <div class="kpi"><div class="k">New problems warned before the shift</div><div class="v g">${num(model.NEW_PROBLEMS_FLAGGED)} of ${num(model.NEW_PROBLEMS)}</div><div class="c">on past shifts the model never learned from</div></div>
      <div class="kpi"><div class="k">Same, with the end-of-line check</div><div class="v r">${num(rule.NEW_PROBLEMS_FLAGGED)} of ${num(rule.NEW_PROBLEMS)}</div><div class="c">it only finds a problem after the shift</div></div>
    </div>`;
  btn.disabled = false;
  btn.textContent = "Run again";
}


/* ---------- 7. order a part: normal search, then deep scan ---------- */
const ORDER_PARTS = ["BEARING-6205", "BEARING-GREASE-BG1", "COOLANT-PUMP-CP4", "COOLANT-FILTER-CF1", "DRIVE-FUSE-F30",
  "SPINDLE-DRIVE-SD2", "GEARBOX-SEAL-GS7", "GEAR-OIL-GO5", "TOOL-INSERT-T12"];
const FILE_FORMAT = {
  A: "spreadsheet (CSV) · prices in ₹ · delivery in days",
  B: "JSON file · prices in US$ · delivery in weeks",
  C: "spreadsheet, other layout · prices as text \"Rs. 1,234.50\" · delivery as text \"3 wks\"",
};
S.order = { part: "BEARING-6205", qty: 1 };

function renderOrderChips() {
  $("ordparts").innerHTML = ORDER_PARTS.map((p) => `<button type="button" class="chip" data-part="${p}" aria-pressed="${p === S.order.part}">${esc(p)}${S.missing && S.missing[p] ? `<span class="need">needed</span>` : ""}</button>`).join("");
  $("ordparts").querySelectorAll(".chip").forEach((b) => b.addEventListener("click", () => {
    S.order.part = b.dataset.part; S.order.qty = (S.missing && S.missing[b.dataset.part]) || 1;
    renderOrderChips(); $("ordbody").innerHTML = ""; sqlNote("order", []);
  }));
}

async function orderSearch() {
  const btn = $("ordsearch");
  btn.disabled = true;
  const body = $("ordbody");
  body.innerHTML = `<div class="scanning small">Looking up ${esc(S.order.part)} in the supplier price lists</div>`;
  let d;
  try { d = await api(`/api/demo/order?part=${encodeURIComponent(S.order.part)}&qty=${S.order.qty}`); }
  catch (e) { body.innerHTML = `<span class="err">${esc(e.message)}</span>`; btn.disabled = false; return; }
  S.orderData = d;
  sqlNote("order", [d.plain]);
  const lines = d.files.rows.reduce((a, f) => a + f.LINES, 0);
  body.innerHTML = `<div class="plain fadein"><span class="zero">${num(d.plain.rows.length)}</span>
      <span>matches for <b>${esc(d.part.PART_NO)}</b> in ${num(d.files.rows.length)} supplier price lists (${num(lines)} lines, ${num(d.plain.ms, 0)} ms).<br>
      The suppliers call it something else, so a normal search finds nothing.</span>
      <button type="button" class="deepbtn" id="deepbtn">Deep scan</button></div><div id="deepbody"></div>`;
  $("deepbtn").addEventListener("click", deepScan);
  btn.disabled = false;
}

async function deepScan() {
  const d = S.orderData;
  $("deepbtn").disabled = true;
  sqlNote("order", [d.plain, d.files, d.deep]);
  const rows = d.deep.rows;
  const files = d.files.rows;
  $("deepbody").innerHTML = `<div class="qctime fadein"><span class="t">${num(d.deep.ms, 0)} ms</span>
      <span class="w">${esc(S.backend)} read all ${num(files.length)} price lists in their own formats, converted prices to ₹ and delivery to days, and compared every line with our part <b>${esc(d.part.PART_NO)}</b> (maker's code <b>${esc(d.part.MATCH_KEY)}</b>)</span></div>
    <div class="files3">${files.map((f) => {
      const mine = rows.filter((r) => r.SUPPLIER_ID === f.SUPPLIER_ID);
      return `<div class="sfile"><div class="nm">${esc(f.SUPPLIER_NAME)}</div><div class="fmt">${esc(FILE_FORMAT[f.SUPPLIER_ID] || f.CATALOG_FORMAT)}</div>
        ${mine.map((r, i) => `<div class="raw ${r.VERDICT}" data-k="${f.SUPPLIER_ID}${i}">${esc(r.RAW_LINE)}</div><div class="vd ${r.VERDICT}" id="vd_${f.SUPPLIER_ID}${i}"></div>`).join("")}
        <div class="skipped">${num(f.LINES - mine.length)} other lines: a different kind of item</div></div>`;
    }).join("")}</div><div id="orderline"></div>`;
  const verdictText = (r) => r.VERDICT === "SAME"
    ? `✓ Same part <small>→ ₹${num(r.UNIT_PRICE_INR, 0)} each · ${num(r.LEAD_DAYS)} days</small>`
    : r.VERDICT === "ASK" ? `? Code almost matches, a person checks it <small>never ordered automatically</small>`
    : `✗ Different part <small>similar name, not ours</small>`;
  const order = [];
  files.forEach((f) => rows.filter((r) => r.SUPPLIER_ID === f.SUPPLIER_ID).forEach((r, i) => order.push([f.SUPPLIER_ID + i, r])));
  for (const [k, r] of order) {
    await sleep(420);
    document.querySelector(`.raw[data-k="${k}"]`).classList.add("on");
    $("vd_" + k).innerHTML = verdictText(r);
  }
  await sleep(500);
  const same = rows.filter((r) => r.VERDICT === "SAME");
  if (!same.length) { $("orderline").innerHTML = `<div class="plain">No supplier sells exactly this part. Ask a buyer to review the near matches.</div>`; return; }
  const cheapest = [...same].sort((a, b) => a.UNIT_PRICE_INR - b.UNIT_PRICE_INR)[0];
  const fastest = [...same].sort((a, b) => a.LEAD_DAYS - b.LEAD_DAYS)[0];
  const pack = Math.max(1, cheapest.ORDER_MULTIPLE || 1);
  const qty = Math.ceil(d.qty / pack) * pack;
  $("orderline").innerHTML = `<div class="orderline fadein"><div class="h">ORDER</div>
    <b>${num(qty)} × ${esc(d.part.PART_NO)}</b> from <b>${esc(cheapest.SUPPLIER_NAME)}</b> (their code ${esc(cheapest.SUPPLIER_CODE)}),
    ₹${num(cheapest.UNIT_PRICE_INR, 0)} each, <b>₹${num(qty * cheapest.UNIT_PRICE_INR, 0)}</b>, arrives in <b>${num(cheapest.LEAD_DAYS)} days</b>${pack > 1 ? ` (sold in packs of ${num(pack)})` : ""}.
    ${fastest.SUPPLIER_ID !== cheapest.SUPPLIER_ID ? `<br><span class="muted">Fastest instead: ${esc(fastest.SUPPLIER_NAME)}, ${num(fastest.LEAD_DAYS)} days, ₹${num(fastest.UNIT_PRICE_INR, 0)} each.</span>` : ""}</div>`;
}


/* ---------- 0. the data as it arrives, then combine ---------- */
const hm = (s) => String(s).slice(11, 16);
function minutesBetween(t0, t1) {
  const out = []; let t = ts(t0); const end = ts(t1);
  while (t <= end) { out.push(t.toTimeString().slice(0, 5)); t = new Date(t.getTime() + 60000); }
  return out;
}
function rawTable(cols, rows, fmt, expected) {
  let body;
  if (expected) {
    const byMin = Object.fromEntries(rows.map((r) => [hm(r[cols[1]]), r]));
    body = expected.map((m) => byMin[m] ? `<tr>${cols.map((c) => `<td>${fmt(c, byMin[m][c])}</td>`).join("")}</tr>`
                                       : `<tr class="gap"><td colspan="${cols.length}">${m} · no reading</td></tr>`).join("");
  } else {
    body = rows.map((r) => `<tr>${cols.map((c) => `<td>${fmt(c, r[c])}</td>`).join("")}</tr>`).join("");
  }
  return `<table class="rawt"><thead><tr>${cols.map((c) => `<th>${esc(c)}</th>`).join("")}</tr></thead><tbody>${body}</tbody></table>`;
}
const rawFmt = (c, v) => (v == null ? "" : /TS|AT_UTC|INTERVAL/.test(c) ? esc(String(v).slice(5, 16)) : typeof v === "number" ? esc(Number(v).toFixed(c === "KWH" ? 3 : c === "RPM" ? 0 : 2)) : esc(v));

async function loadSources() {
  try {
    const d = await api("/api/demo/sources");
    S.src = d;
    const r = d.registry.rows[0];
    $("srcnames").innerHTML = `<span class="mid">${esc(d.machine)}</span>is called:
      <span class="nm">production system <b>${esc(r.ASSET_TAG)}</b></span><span class="nm">condition monitor <b>${esc(r.CM_SENSOR_ID)}</b></span>
      <span class="nm">PLC <b>${esc(r.PLC_TAG)}</b></span><span class="nm">drive <b>${esc(r.DRIVE_ID)}</b></span><span class="nm">energy meter <b>${esc(r.METER_ID)}</b></span>`;
    const exp = minutesBetween(d.t0, d.t1);
    const lastE = d.energy.rows[0];
    const eEnd = lastE ? new Date(ts(lastE.INTERVAL_START_UTC).getTime() + 15 * 60000) : null;
    const lateMin = eEnd ? Math.round((ts(d.t1) - eEnd) / 60000) : null;
    const pulled = ts(d.data_as_of);
    $("srccards").innerHTML = `
      <div class="scard"><div class="nm">Condition monitor</div><div class="iss"><i>UTC</i><i>every minute</i><i class="bad">gaps</i></div>
        ${rawTable(["SENSOR_ID", "TS_UTC", "VIB_RMS"], d.vibration.rows, rawFmt, exp)}</div>
      <div class="scard"><div class="nm">PLC historian</div><div class="iss"><i>India time (+5:30)</i><i>°C</i><i>every 5 min</i></div>
        ${rawTable(["PLC_TAG", "TS_LOCAL", "AIR_C", "PROCESS_C"], d.temps.rows, rawFmt)}
        <div class="foot">Same minutes as the others, but the clock reads 5 h 30 min ahead.</div></div>
      <div class="scard"><div class="nm">Drive controller</div><div class="iss"><i>UTC</i><i>every minute</i><i class="bad">gaps</i></div>
        ${rawTable(["DRIVE_ID", "TS_UTC", "RPM", "TORQUE_NM"], d.drive.rows, rawFmt, exp)}</div>
      <div class="scard"><div class="nm">Energy meter</div><div class="iss"><i>kWh per 15 min</i><i class="bad">arrives late</i></div>
        ${rawTable(["METER_ID", "INTERVAL_START_UTC", "KWH", "RECEIVED_AT_UTC"], d.energy.rows, rawFmt)}
        <div class="foot">${lateMin != null && lateMin > 0 ? `No readings after <b>${esc(eEnd.toTimeString().slice(0, 5))}</b> yet: not delivered when the data was pulled at ${esc(pulled.toTimeString().slice(0, 5))}.` : "Readings arrive in 15-minute blocks, hours after the fact."}</div></div>
      <div class="scard"><div class="nm">Production system</div><div class="iss"><i>India time (+5:30)</i><i class="bad">logs every 5–25 min</i></div>
        ${rawTable(["ASSET_TAG", "EVENT_TS_LOCAL", "TOOL_WEAR_MIN"], d.tool.rows, rawFmt)}</div>`;
    sqlNote("src", [d.registry, d.vibration, d.temps, d.drive, d.energy, d.tool]);
    $("combine").disabled = false;
  } catch (e) {
    $("srccards").innerHTML = `<span class="err">${esc(e.message)}</span>`;
    unlockDemo();
  }
}

function unlockDemo() { $("picksect").classList.remove("locked"); }

async function combine() {
  const btn = $("combine");
  btn.disabled = true;
  $("fusedbody").innerHTML = `<div class="scanning small">Combining every machine's five systems in ${esc(S.backend)}</div>`;
  let d;
  try { d = await fetch("/api/demo/combine", { method: "POST" }).then(async (r) => { const b = await r.json(); if (!r.ok) throw new Error(b.detail || r.status); return b; }); }
  catch (e) { $("fusedbody").innerHTML = `<span class="err">${esc(e.message)}</span>`; btn.disabled = false; unlockDemo(); return; }
  const q = d.quality;
  const cols = [["VIBRATION_MM_S", "VIB_Q", "Vibration", "mm/s", 2], ["AIR_TEMP_K", "TMP_Q", "Air", "K", 1], ["PROCESS_TEMP_K", "TMP_Q", "Process", "K", 1],
    ["RPM", "DRV_Q", "Speed", "rpm", 0], ["TORQUE_NM", "DRV_Q", "Torque", "Nm", 1], ["POWER_KW", "PWR_Q", "Power", "kW", 2], ["TOOL_WEAR_MIN", "TOOL_Q", "Tool wear", "min", 1]];
  $("fusedbody").innerHTML = `<div class="qctime fadein"><span class="t">${num(d.combine.ms / 1000, 1)} s</span>
      <span class="w">${esc(S.backend)} combined <b>${big(d.raw_rows)}</b> rows from 5 systems into <b>${big(d.minutes)}</b> aligned minutes, for every machine</span></div>
    <div class="minihead">${esc(d.machine)}, the same minutes, combined</div>
    <div class="tblwrap"><table class="fusedt"><thead><tr><th>Time<small>UTC</small></th>${cols.map((c) => `<th>${c[2]}<small>${c[3]}</small></th>`).join("")}</tr></thead><tbody>
    ${d.fused.rows.map((r) => `<tr><td>${esc(hm(r.READING_TS))}</td>${cols.map((c) => {
        const v = r[c[0]], qf = r[c[1]];
        const cls = qf === 1 ? "" : qf === 0 ? "f" : "m";
        return `<td class="${cls}">${v == null ? "—" : num(v, c[4])}</td>`;
      }).join("")}</tr>`).join("")}
    </tbody></table></div>
    <div class="legend2"><span>white: measured</span><span style="color:#fcd34d">yellow ↻: filled from the reading just before</span><span style="color:#fca5a5">red —: still missing</span>
      <span>whole plant: <b>${pct(q.MEASURED / q.CELLS)}</b> measured, <b>${pct(q.FILLED / q.CELLS)}</b> filled</span></div>
    <div class="nextstep">Every stream now lines up minute by minute. Next: find the fault patterns ↓</div>`;
  sqlNote("src", [d.combine]);
  const rows = [...document.querySelectorAll(".fusedt tbody tr")];
  for (const tr of rows) { tr.classList.add("in"); await sleep(60); }
  unlockDemo();
  btn.textContent = "Combine again";
  btn.disabled = false;
}

/* ---------- the flow ---------- */
async function run(mode) {
  const f = FAULTS.find((x) => x.mode === mode);
  const runId = ++S.run;
  S.fault = f;
  setBusy(true);
  $("faults").querySelectorAll(".fault").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.mode === mode)));
  $("kitsect").hidden = true;
  $("livenote").textContent = ""; $("selected").hidden = true; $("strips").innerHTML = ""; $("liveclock").textContent = ""; $("livemachine").textContent = "";
  try {
    const replay = api("/api/demo/replay?mode=" + encodeURIComponent(mode));
    renderScanning(S.rowsHint);
    const d = await api("/api/demo/search?mode=" + encodeURIComponent(mode));
    if (runId !== S.run) return;
    S.rule = { ...S.rule, ...d.rule };
    S.rowsHint = d.rows_scanned;
    $("src").textContent = `${big(d.rows_scanned)} readings`;
    renderSignature(f, d.signature.rows);
    renderSearch(f, d);
    const r = await replay;
    if (runId !== S.run) return;
    sqlNote("replay", [r.pick, r.stream, r.kit].filter(Boolean));
    if (!r.stream) { $("livenote").textContent = "No held-out failure of this kind was named correctly, so there is nothing to replay."; setBusy(false); return; }
    playReplay(f, r, runId);
  } catch (e) {
    $("searchbody").innerHTML = `<span class="err">${esc(e.message)}</span>`;
    setBusy(false);
  }
}

(async () => {
  renderButtons();
  $("replay").addEventListener("click", () => S.fault && run(S.fault.mode));
  $("qcrun").addEventListener("click", runQualityCheck);
  renderOrderChips();
  $("combine").addEventListener("click", combine);
  loadSources();
  $("ordsearch").addEventListener("click", orderSearch);
  try {
    const h = await api("/api/health");
    S.backend = h.backend || "Exasol";
    $("sys").textContent = "ONLINE"; $("sys").className = "v on";
  } catch (e) {
    $("sys").textContent = "OFFLINE"; $("sys").className = "v off";
    $("searchbody").innerHTML = `<span class="err">Can't reach the database: ${esc(e.message)}</span>`;
  }
})();
