"use strict";

const $ = (id) => document.getElementById(id);
const store = {
  get(k, d) { try { return localStorage.getItem(k) ?? d; } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* ignore */ } },
};

let unit = store.get("unit", "C");
let span = Number(store.get("span", 1800));
let last = null;            // last /api/status
let history = [];
let protocol = null;
let spDirty = false;        // user is editing the setpoint input
let cfgDirty = false;

// ---------------------------------------------------------------- helpers
async function api(path, body) {
  const opts = body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  };
  const r = await fetch(path, opts);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || `${r.status} ${r.statusText}`);
  return data;
}
const toF = (c) => c * 9 / 5 + 32;
const fromDisp = (v) => unit === "F" ? (v - 32) * 5 / 9 : v;
const tDisp = (c) => unit === "F" ? toF(c) : c;          // absolute temperature
const dDisp = (c) => unit === "F" ? c * 9 / 5 : c;        // temperature difference
const uLabel = () => unit === "F" ? "°F" : "°C";
const fmt = (v, d = 2) => (v == null || Number.isNaN(v)) ? "—" : v.toFixed(d);
const withUnit = (txt, u) => `${txt}<span class="u">${u}</span>`;

function toast(msg, bad = false) {
  const t = $("toast");
  t.textContent = msg;
  t.className = "toast show" + (bad ? " bad" : "");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.className = "toast", bad ? 6000 : 3000);
}

function confirmDialog(title, html, okLabel = "Write") {
  return new Promise((resolve) => {
    const d = $("confirmDlg");
    $("confirmTitle").textContent = title;
    $("confirmBody").innerHTML = html;
    $("confirmOk").textContent = okLabel;
    d.returnValue = "";
    d.addEventListener("close", () => resolve(d.returnValue === "ok"), { once: true });
    d.showModal();
  });
}

function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// ---------------------------------------------------------------- ports
async function loadPorts() {
  const sel = $("portSelect");
  const prev = sel.value || store.get("port", "");
  let ports = [];
  try { ports = await api("/api/ports"); } catch { /* ignore */ }
  sel.innerHTML = "";
  for (const p of ports) {
    const o = document.createElement("option");
    o.value = p.device;
    o.textContent = p.description && p.description !== "n/a" ? `${p.device} — ${p.description}` : p.device;
    sel.appendChild(o);
  }
  const custom = document.createElement("option");
  custom.value = "__custom__"; custom.textContent = "Other path…";
  sel.appendChild(custom);
  const sim = document.createElement("option");
  sim.value = "__sim__"; sim.textContent = "Simulator (no hardware)";
  sel.appendChild(sim);
  if ([...sel.options].some((o) => o.value === prev)) sel.value = prev;
}

let portSynced = false;
function syncPortSelect(c) {
  // Show the port the server is actually using (once per connection).
  if (!c.connected) { portSynced = false; return; }
  if (portSynced) return;
  const sel = $("portSelect");
  const want = c.simulated ? "__sim__" : c.port;
  if (want && ![...sel.options].some((o) => o.value === want)) {
    const o = document.createElement("option");
    o.value = want; o.textContent = want;
    sel.insertBefore(o, sel.firstChild);
  }
  if (want) sel.value = want;
  portSynced = true;
}

async function connect() {
  let v = $("portSelect").value;
  let body;
  if (v === "__sim__") body = { simulate: true };
  else {
    if (v === "__custom__") {
      v = prompt("Serial device path", store.get("customPort", "/dev/ttyUSB0"));
      if (!v) return;
      store.set("customPort", v);
    }
    body = { port: v };
  }
  store.set("port", $("portSelect").value);
  try {
    last = await api("/api/connect", body);
    history = []; portSynced = false;
    render();
    toast("Connected");
  } catch (e) { toast(`Connect failed: ${e.message}`, true); }
}

// ---------------------------------------------------------------- render
function renderLamps(el, defs, bits, badKeys) {
  el.innerHTML = defs.map((d) => {
    const on = bits ? bits[d.key] : null;
    const cls = ["lamp", on == null ? "unknown" : on ? "on" : "", badKeys(d) ? "bad" : ""].join(" ");
    return `<div class="${cls}"><i></i>${esc(d.label)}</div>`;
  }).join("");
}

function render() {
  const s = last;
  if (!s) return;
  const c = s.connection;
  const v = s.values;
  const u = uLabel();

  // connection
  const dot = $("linkDot");
  dot.className = "dot " + ({ ok: c.stale ? "warn" : "ok", no_response: "bad", connecting: "warn" }[c.state] || "");
  $("linkText").textContent = c.connected
    ? `${c.transport} · ${c.state.replace("_", " ")}${c.comm_errors ? ` · ${c.comm_errors} comm errors` : ""}`
    : "Disconnected";
  syncPortSelect(c);
  $("disconnectBtn").disabled = !c.connected;

  // banner
  const msgs = [];
  let level = "warn";
  if (c.connected && c.state === "no_response") { msgs.push(`Chiller not responding: ${c.last_error || ""}`); level = "bad"; }
  else if (c.connected && c.stale && c.last_ok) msgs.push("Data is stale");
  if (s.faults && s.faults.raw) { msgs.push(`FAULT: ${s.faults.active.join(", ")}`); level = "bad"; }
  if (s.health && s.health.raw) { msgs.push(`EEPROM: ${s.health.active.join(", ")}`); level = "bad"; }
  if (s.firmware && !s.firmware.ok) msgs.push(`Unexpected firmware ID 0x${s.firmware.raw.toString(16).toUpperCase()}`);
  const b = $("banner");
  b.hidden = msgs.length === 0;
  b.className = "banner " + level;
  b.textContent = msgs.join(" · ");

  // readouts
  document.querySelector(".readouts").classList.toggle("stale", !v || c.stale);
  $("pv").innerHTML = v ? withUnit(fmt(tDisp(v.pv_c), 2), u) : "—";
  $("sp").innerHTML = v ? withUnit(fmt(tDisp(v.setpoint_c), 1), u) : "—";
  const e = v ? dDisp(v.error_c) : null;
  $("err").innerHTML = v ? withUnit((e > 0 ? "+" : "") + fmt(e, 2), u) : "—";
  $("out").innerHTML = v ? withUnit(fmt(v.pid_output_pct, 1), "%") : "—";
  $("outBar").style.width = v ? `${Math.max(0, Math.min(100, v.pid_output_pct))}%` : "0";
  const d = s.derived;
  $("pvFoot").textContent = d
    ? [d.run_requested ? "Run requested" : "Idle (no REMOTE)", d.compressor ? "compressor on" : "compressor off",
       d.flow_ok ? "flow OK" : "no flow"].join(" · ")
    : " ";
  $("spFoot").textContent = s.config ? `EEPROM: ${fmt(tDisp(s.config.setpoint_c), 1)} ${u}` : " ";

  // setpoint editor
  const lim = s.limits;
  const writable = c.connected && !lim.read_only;
  $("spApply").disabled = !writable;
  document.querySelectorAll(".spform .step").forEach((x) => x.disabled = !writable);
  $("spInput").disabled = !writable;
  if (!spDirty && v) $("spInput").value = fmt(tDisp(v.setpoint_c), 1);
  document.querySelectorAll("[data-unit-label]").forEach((x) => x.textContent = u);
  $("spHint").textContent = lim.read_only ? "Server is in read-only mode."
    : `Allowed range ${fmt(tDisp(lim.setpoint_min), 1)}–${fmt(tDisp(lim.setpoint_max), 1)} ${u}. Stored in 0.1 °C steps.`;

  // lamps
  if (protocol) {
    renderLamps($("lamps"), protocol.opto_bits.filter((x) => !["set", "run", "spare"].includes(x.key)),
      s.opto && s.opto.bits, (x) => x.bad);
    renderLamps($("faultLamps"), protocol.fault_bits, s.faults && s.faults.bits, () => true);
  }
  $("optoRaw").textContent = s.opto ? `C0 = 0x${s.opto.raw.toString(16).toUpperCase().padStart(3, "0")}` : "";

  // PID table
  if (v) {
    const rows = [
      ["P term", v.p_term], ["I term", v.i_term], ["D term", v.d_term],
      ["Integral accumulator", v.integral_acc], ["Δ error / update", v.delta_error],
    ];
    $("pidTable").innerHTML = rows.map(([k, x]) => `<tr><td>${k}</td><td>${fmt(x, 3)}</td></tr>`).join("");
  }
  renderConfig(s);
  $("sysTable").innerHTML = [
    ["Firmware ID (A0)", s.firmware ? `0x${s.firmware.raw.toString(16).toUpperCase()} ${s.firmware.ok ? "✓" : "⚠"}` : "—"],
    ["EEPROM health (B0)", s.health ? (s.health.raw ? s.health.active.join(", ") : "OK") : "—"],
    ["Polls / comm errors", `${c.polls} / ${c.comm_errors}`],
  ].map(([k, x]) => `<tr><td>${k}</td><td>${esc(x)}</td></tr>`).join("");

  // sim
  $("simCard").hidden = !c.simulated;
  if (s.sim) $("simInfo").textContent =
    `REMOTE ${s.sim.remote ? "on" : "off"} · conditions: ${s.sim.injected.join(", ") || "none"} · EEPROM writes: ${s.sim.eeprom_writes}`;

  drawChart();
}

const CFG_LABELS = {
  proportional_band: "Proportional band",
  integral_time: "Integral time",
  derivative_time: "Derivative time",
  unused: "(unused)",
  pv_filter: "PV filter",
  pv_gain_trim: "PV gain trim (%)",
  pv_offset_trim: "PV offset trim",
  setpoint_x10: "Setpoint ×10 (°C)",
};

function renderConfig(s) {
  const cfg = s.config;
  const editable = s.limits.editable_config;
  const writable = s.connection.connected && !s.limits.read_only;
  $("cfgApply").disabled = !writable || !cfg;
  if (cfgDirty) return;
  if (!cfg) { $("cfgTable").innerHTML = "<tr><td>Not read yet</td><td></td></tr>"; return; }
  $("cfgTable").innerHTML = Object.keys(CFG_LABELS).map((k) => {
    const [lo, hi] = editable[k] || [];
    const cell = editable[k] && writable
      ? `<input type="number" step="1" min="${lo}" max="${hi}" name="${k}" value="${cfg[k]}" data-orig="${cfg[k]}">`
      : esc(cfg[k]);
    return `<tr><td>${CFG_LABELS[k]}</td><td>${cell}</td></tr>`;
  }).join("");
}

// ---------------------------------------------------------------- chart
function cssVar(n) { return getComputedStyle(document.documentElement).getPropertyValue(n).trim(); }

function drawChart() {
  const cv = $("chart");
  const dpr = window.devicePixelRatio || 1;
  const W = cv.clientWidth, H = cv.clientHeight || 280;
  if (cv.width !== Math.round(W * dpr) || cv.height !== Math.round(H * dpr)) {
    cv.width = Math.round(W * dpr); cv.height = Math.round(H * dpr);
  }
  const g = cv.getContext("2d");
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, W, H);
  const muted = cssVar("--muted"), grid = cssVar("--grid");
  g.font = "11px system-ui, sans-serif";

  const now = Date.now() / 1000;
  const t0 = now - span;
  const pts = history.filter((p) => p.t >= t0);
  const L = 46, R = 40, T = 10, B = 22;
  const pw = W - L - R, ph = H - T - B;

  if (pts.length < 2) {
    g.fillStyle = muted; g.textAlign = "center";
    g.fillText(last && last.connection.connected ? "Collecting data…" : "Not connected", W / 2, H / 2);
    return;
  }
  let lo = Infinity, hi = -Infinity;
  for (const p of pts) { lo = Math.min(lo, p.pv, p.sp); hi = Math.max(hi, p.pv, p.sp); }
  lo = tDisp(lo); hi = tDisp(hi);
  const pad = Math.max(0.25, (hi - lo) * 0.15);
  lo -= pad; hi += pad;
  const x = (t) => L + (t - t0) / span * pw;
  const y = (v) => T + (1 - (v - lo) / (hi - lo)) * ph;
  const y2 = (o) => T + (1 - o / 100) * ph;

  // grid + axes labels
  const step = niceStep((hi - lo) / 5);
  g.strokeStyle = grid; g.lineWidth = 1; g.fillStyle = muted; g.textAlign = "right"; g.textBaseline = "middle";
  for (let v = Math.ceil(lo / step) * step; v <= hi; v += step) {
    g.beginPath(); g.moveTo(L, y(v)); g.lineTo(W - R, y(v)); g.stroke();
    g.fillText(v.toFixed(step < 1 ? (step < 0.1 ? 2 : 1) : 0), L - 6, y(v));
  }
  g.textAlign = "left";
  for (const o of [0, 50, 100]) g.fillText(o + "%", W - R + 6, y2(o));
  g.textAlign = "center"; g.textBaseline = "top";
  const tstep = span <= 300 ? 60 : span <= 1800 ? 300 : span <= 7200 ? 1800 : 3600;
  let lastLabelX = -Infinity;
  for (let t = Math.ceil(t0 / tstep) * tstep; t <= now; t += tstep) {
    const label = new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    const X = x(t), w = g.measureText(label).width;
    if (X - w / 2 < lastLabelX + 8 || X + w / 2 > W) continue;   // skip overlapping labels
    g.fillText(label, X, H - B + 6);
    lastLabelX = X + w / 2;
  }
  g.save(); g.translate(12, T + ph / 2); g.rotate(-Math.PI / 2); g.textBaseline = "middle";
  g.fillText(uLabel(), 0, 0); g.restore();

  const line = (color, width, fy, dash) => {
    g.strokeStyle = color; g.lineWidth = width; g.setLineDash(dash || []); g.lineJoin = "round";
    g.beginPath();
    let prev = null;
    for (const p of pts) {
      const X = x(p.t), Y = fy(p);
      if (prev && p.t - prev > 10) g.moveTo(X, Y); else g.lineTo(X, Y);  // gaps for outages
      prev = p.t;
    }
    g.stroke(); g.setLineDash([]);
  };
  line(cssVar("--out"), 1, (p) => y2(p.out));
  line(cssVar("--sp"), 1.5, (p) => y(tDisp(p.sp)), [5, 4]);
  line(cssVar("--pv"), 2, (p) => y(tDisp(p.pv)));
}

function niceStep(raw) {
  const p = Math.pow(10, Math.floor(Math.log10(raw || 1)));
  const n = raw / p;
  return (n < 1.5 ? 1 : n < 3.5 ? 2 : n < 7.5 ? 5 : 10) * p;
}

// ---------------------------------------------------------------- events
async function refreshEvents() {
  try {
    const ev = await api("/api/events");
    $("events").innerHTML = ev.slice(0, 200).map((e) =>
      `<li class="${e.level}"><time>${new Date(e.t * 1000).toLocaleTimeString()}</time><span>${esc(e.msg)}</span></li>`
    ).join("") || '<li class="muted">No events yet</li>';
  } catch { /* ignore */ }
}

// ---------------------------------------------------------------- loop
let lastHistT = 0;
async function tick() {
  try {
    last = await api("/api/status");
    if (!history.length) {
      history = await api(`/api/history?seconds=${21600}`);
    } else {
      const fresh = await api(`/api/history?seconds=${Math.max(5, Date.now() / 1000 - lastHistT + 2)}`);
      for (const p of fresh) if (p.t > lastHistT) history.push(p);
      const cut = Date.now() / 1000 - 21600;
      while (history.length && history[0].t < cut) history.shift();
    }
    if (history.length) lastHistT = history[history.length - 1].t;
    render();
  } catch (e) {
    $("linkDot").className = "dot bad";
    $("linkText").textContent = `Web server unreachable (${e.message})`;
  }
}

// ---------------------------------------------------------------- wiring
function setUnit(u) {
  unit = u; store.set("unit", u); spDirty = false;
  document.querySelectorAll("[data-unit]").forEach((b) => b.classList.toggle("on", b.dataset.unit === u));
  render();
}

function wire() {
  document.querySelectorAll("[data-unit]").forEach((b) => b.addEventListener("click", () => setUnit(b.dataset.unit)));
  document.querySelectorAll("#spanSeg button").forEach((b) => {
    b.classList.toggle("on", Number(b.dataset.span) === span);
    b.addEventListener("click", () => {
      span = Number(b.dataset.span); store.set("span", span);
      document.querySelectorAll("#spanSeg button").forEach((x) => x.classList.toggle("on", x === b));
      drawChart();
    });
  });
  $("refreshPorts").addEventListener("click", loadPorts);
  $("connectBtn").addEventListener("click", connect);
  $("disconnectBtn").addEventListener("click", async () => {
    try { last = await api("/api/disconnect", {}); render(); } catch (e) { toast(e.message, true); }
  });

  const spIn = $("spInput");
  spIn.addEventListener("input", () => spDirty = true);
  spIn.addEventListener("keydown", (e) => { if (e.key === "Escape") { spDirty = false; render(); } });
  document.querySelectorAll(".spform .step").forEach((b) => b.addEventListener("click", () => {
    const cur = parseFloat(spIn.value) || 0;
    spIn.value = (cur + Number(b.dataset.step) * 0.1).toFixed(1);
    spDirty = true;
  }));
  $("spForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const disp = parseFloat(spIn.value);
    if (Number.isNaN(disp)) return toast("Enter a number", true);
    const c = Math.round(fromDisp(disp) * 10) / 10;
    const lim = last.limits;
    if (c < lim.setpoint_min || c > lim.setpoint_max) {
      return toast(`Out of range (${lim.setpoint_min}–${lim.setpoint_max} °C)`, true);
    }
    const cur = last.values ? last.values.setpoint_c : null;
    const ok = await confirmDialog("Change setpoint?",
      `<p class="diff">${cur == null ? "?" : fmt(tDisp(cur), 1)} → <b>${fmt(tDisp(c), 1)}</b> ${uLabel()}` +
      (unit === "F" ? ` <span class="muted">(stored as ${c.toFixed(1)} °C)</span>` : "") + `</p>` +
      `<p class="muted">This writes the chiller's EEPROM.</p>`);
    if (!ok) return;
    try {
      const r = await api("/api/setpoint", { celsius: c });
      spDirty = false;
      toast(r.changed ? `Setpoint set to ${fmt(tDisp(c), 1)} ${uLabel()} (verified)` : "Setpoint unchanged");
      tick(); refreshEvents();
    } catch (err) { toast(`Setpoint write failed: ${err.message}`, true); }
  });

  $("cfgForm").addEventListener("input", (e) => {
    if (e.target.matches("input")) {
      cfgDirty = true;
      e.target.classList.toggle("changed", e.target.value !== e.target.dataset.orig);
    }
  });
  $("cfgForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const changes = {};
    const lines = [];
    for (const inp of $("cfgForm").querySelectorAll("input")) {
      if (inp.value === inp.dataset.orig) continue;
      const n = Number(inp.value);
      if (!Number.isInteger(n)) return toast(`${CFG_LABELS[inp.name]} must be an integer`, true);
      changes[inp.name] = n;
      lines.push(`${esc(CFG_LABELS[inp.name])}: ${esc(inp.dataset.orig)} → <b>${n}</b>`);
    }
    if (!lines.length) { cfgDirty = false; return toast("No changes"); }
    const ok = await confirmDialog("Write PID configuration?",
      `<p class="diff">${lines.join("<br>")}</p><p class="warn">The firmware does no range checking. Make sure these values are right.</p>`,
      "Write to EEPROM");
    if (!ok) return;
    try {
      const r = await api("/api/config", { changes, confirm: true });
      cfgDirty = false;
      toast(r.changed ? "Config written and verified" : "Config unchanged");
      tick(); refreshEvents();
    } catch (err) { toast(`Config write failed: ${err.message}`, true); }
  });
  $("cfgRevert").addEventListener("click", () => { cfgDirty = false; render(); });

  $("rawForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const out = $("rawOut");
    try {
      const r = await api("/api/raw", { command: $("rawCmd").value });
      const dec = typeof r.decoded === "object" ? JSON.stringify(r.decoded, null, 1) : r.decoded;
      out.textContent = `> ${r.command}\nTX ${r.tx}\nRX ${r.rx}\n${dec}\n\n` + out.textContent;
    } catch (err) {
      out.textContent = `> ${$("rawCmd").value}\n! ${err.message}\n\n` + out.textContent;
    }
  });

  $("simRemote").addEventListener("click", async () => {
    try { await api("/api/sim", { remote: !(last.sim && last.sim.remote) }); tick(); } catch (e) { toast(e.message, true); }
  });
  $("simInject").addEventListener("click", async () => {
    try { await api("/api/sim", { inject: $("simFault").value }); tick(); } catch (e) { toast(e.message, true); }
  });
  $("simClear").addEventListener("click", async () => {
    try { await api("/api/sim", { clear: true }); tick(); } catch (e) { toast(e.message, true); }
  });

  window.addEventListener("resize", drawChart);
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", drawChart);
}

async function init() {
  wire();
  setUnit(unit);
  try {
    protocol = await api("/api/protocol");
    $("rawCmd").innerHTML = Object.entries(protocol.read_commands)
      .map(([k, d]) => `<option value="${k}">${k}: ${esc(d)}</option>`).join("");
    $("simFault").innerHTML = protocol.fault_bits
      .map((f) => `<option value="${f.key}">${esc(f.label)}</option>`).join("");
  } catch { /* server down; tick() will report */ }
  await loadPorts();
  await tick();
  refreshEvents();
  setInterval(tick, 1000);
  setInterval(refreshEvents, 3000);
}

init();
