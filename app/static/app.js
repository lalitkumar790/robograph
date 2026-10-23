const $ = (s, el = document) => el.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const when = ts => new Date(ts * 1000).toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
const words = s => String(s || "").replace(/_/g, " ");
const label = r => words(r.action) + (r.target ? " → " + r.target : "");
const post = (path, body) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
let selected = null, busy = false, budget = 180;

async function api(path, opts) {
  const res = await fetch(path, opts);
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.error || (body.detail ? "invalid request" : `HTTP ${res.status}`));
  return body;
}

async function loadRobots() {
  const list = $("#robots");
  try {
    const robots = await api("/api/robots");
    list.innerHTML = robots.map(r => `
      <li data-id="${esc(r.id)}" class="${r.id === selected ? "sel" : ""}">
        <span class="dot ${esc(r.status)}"></span>
        <span><strong>${esc(r.id)}</strong> <span class="muted">${esc(r.name)}</span><br><span class="muted">at ${esc(r.location)}${r.status !== "ok" ? " · " + esc(words(r.status)) : ""}</span></span>
        <span class="mono muted">${r.telemetry ? esc(r.telemetry.rssi) + " dBm" : ""}</span>
      </li>`).join("");
    list.querySelectorAll("li").forEach(li => li.onclick = () => select(li.dataset.id));
  } catch (e) {
    list.innerHTML = `<li class="err">${esc(e.message)}</li>`;
  }
}

async function loadHealth() {
  try {
    const h = await api("/api/health");
    budget = h.budget_s || budget;
    $("#health").textContent = `graph ${h.graph} · ${h.nodes} nodes · LLM ${h.llm_configured ? "configured" : "not configured"}`;
  } catch (e) {
    $("#health").innerHTML = `<span class="err">${esc(e.message)}</span>`;
  }
}

const badge = (v, text) => `<span class="badge ${esc(v)}">${esc(text ?? v)}</span>`;
const evidence = (items, cls) => items.length ? `<ul class="ev ${cls}">${items.map(i => `<li>${esc(i.text)}</li>`).join("")}</ul>` : '<p class="muted small">none recorded</p>';
const cypherBlock = c => !c || !c.length ? "" : `<details><summary>Cypher executed (${c.length} queries)</summary>${c.map(q => `<div><div class="muted small">${esc(q.name)} ${esc(JSON.stringify(q.params))}</div><pre>${esc(q.cypher)}</pre></div>`).join("")}</details>`;

function renderAssessment(a) {
  if (!a.issue) return `<p class="muted">No issue detected: ${esc(a.recommended.basis)}.</p>`;
  const options = a.options.map(o => `
    <div class="opt">
      <div class="ohead"><strong>${esc(label(o))}</strong> ${badge(o.verdict)}${o.avg_rssi != null ? `<span class="muted small">avg ${esc(o.avg_rssi)} dBm</span>` : ""}</div>
      <p class="small reason">${esc(o.reason)}</p>
      <div class="cols">
        <div><div class="lbl">Evidence for</div>${evidence(o.evidence_for, "for")}</div>
        <div><div class="lbl">Evidence against</div>${evidence(o.evidence_against, "against")}</div>
      </div>
      ${o.gaps.length ? `<p class="muted small"><strong>Missing:</strong> ${esc(o.gaps.join("; "))}</p>` : ""}
    </div>`).join("");
  return `<p><span class="issue">${esc(words(a.issue.type))}</span> ${esc(a.issue.detail)}</p>${options || '<p class="muted">No candidate options.</p>'}
    <p class="reco"><strong>Evidence engine proposes:</strong> ${esc(label(a.recommended))} <span class="muted small">(a proposal, nothing is recorded yet)</span><br><span class="muted small">${esc(a.recommended.basis)}</span></p>
    <details><summary>What the graph cannot tell</summary><ul>${a.limitations.map(l => `<li>${esc(l)}</li>`).join("")}</ul></details>${cypherBlock(a.cypher)}`;
}

async function loadAssessment(id) {
  const box = $("#assessment");
  try {
    const a = await api(`/api/robots/${id}/assessment`);
    box.innerHTML = "<h2>Evidence engine recommendation</h2>" + renderAssessment(a);
    const pick = $("#cfloc");
    if (pick && a.recommended.target) pick.value = a.recommended.target;
  } catch (e) {
    box.innerHTML = `<p class="err">${esc(e.message)}</p>`;
  }
}

async function select(id) {
  selected = id;
  document.querySelectorAll("#robots li").forEach(li => li.classList.toggle("sel", li.dataset.id === id));
  const box = $("#detail");
  try {
    const c = await api(`/api/robots/${id}`);
    const t = c.telemetry_latest_first;
    box.innerHTML = `
      <div class="panel">
        <div class="head">
          <h1>${esc(c.robot.id)} <span class="muted">${esc(c.robot.name)}</span></h1>
          <span>at <strong>${esc(c.location)}</strong>, adjacent: <span class="mono">${esc(c.adjacent_locations.join(", "))}</span></span>
          <span>mission: <span class="mono">${c.active_mission ? esc(c.active_mission.id + " " + c.active_mission.route.join("→")) : "none"}</span></span>
        </div>
        <p class="${c.issues.length ? "issue" : "muted"}">${c.issues.length ? c.issues.map(i => esc(words(i.type) + ": " + i.detail)).join("; ") : "No telemetry issues detected."}</p>
        <table><tr><th>time</th><th>location</th><th>RSSI</th><th>battery</th><th>motor temp</th></tr>
        ${t.map(x => `<tr><td>${when(x.ts)}</td><td class="mono">${esc(x.location)}</td><td>${esc(x.rssi)} dBm</td><td>${esc(x.battery)}%</td><td>${esc(x.temp_c)} °C</td></tr>`).join("")}</table>
      </div>
      <div class="panel" id="assessment"><h2>Evidence engine recommendation</h2><p class="muted">Loading…</p></div>
      <div class="panel"><div class="row">
        <input type="text" id="question" maxlength="300" placeholder="Optional question for the agent (default: find the problem and recommend an action)">
        <button id="go">Investigate ${esc(id)}</button>
      </div></div>
      <div id="result"></div>
      <div class="panel" id="cf">
        <h2>Counterfactual</h2>
        <p class="muted small">Copies the graph, changes one thing, and re-runs the evidence checks. The original graph is not modified.</p>
        <div class="row">
          <select id="cfchange"><option value="remove_recoveries">Remove recovery evidence for moves to</option><option value="add_failure">Add a failed incident at</option></select>
          <select id="cfloc">${c.adjacent_locations.map(l => `<option>${esc(l)}</option>`).join("")}</select>
          <label class="small"><input type="checkbox" id="cfagent"> also run the agent</label>
          <button class="secondary" id="cfgo">Run counterfactual</button>
        </div>
        <div id="cfout"></div>
      </div>
      <div class="panel" id="memory"></div>`;
    $("#go").onclick = () => investigate(id);
    $("#cfgo").onclick = () => counterfactual(id);
    loadAssessment(id);
    loadMemory(id);
  } catch (e) {
    box.innerHTML = `<p class="err">${esc(e.message)}</p>`;
  }
}

async function loadMemory(id) {
  const box = $("#memory");
  try {
    const m = await api(`/api/robots/${id}/memory`);
    box.innerHTML = `
      <h2>Memory in graph</h2>
      <div class="grid">
        <div><strong>Previous decisions</strong>
          <ul>${m.previous_decisions.map(d => `<li class="muted"><span class="mono">${esc(d.id)}</span> ${when(d.ts)}: ${esc(words(d.action))}${d.target ? " → " + esc(d.target) : ""} · ${esc(d.status)}${d.outcome ? ", " + esc(words(d.outcome)) : ""}</li>`).join("") || '<li class="muted">None yet.</li>'}</ul></div>
        <div><strong>Operator notes</strong>
          <ul>${m.operator_notes_untrusted_text.map(n => `<li class="muted">${when(n.ts)}: ${esc(n.text)}</li>`).join("") || '<li class="muted">None.</li>'}</ul>
          <div class="row top"><input type="text" id="note" maxlength="300" placeholder="Add a note"><button class="secondary" id="addnote">Save</button></div>
          <div id="noteerr" class="err"></div></div>
      </div>`;
    $("#addnote").onclick = async () => {
      const text = $("#note").value.trim();
      if (!text) return;
      try { await post(`/api/robots/${id}/notes`, { text }); loadMemory(id); } catch (e) { $("#noteerr").textContent = e.message; }
    };
  } catch (e) {
    box.innerHTML = `<p class="err">${esc(e.message)}</p>`;
  }
}

function setBusy(on) {
  busy = on;
  ["#go", "#cfgo"].forEach(sel => { const el = $(sel); if (el) el.disabled = on; });
}

async function investigate(id) {
  if (busy) return;
  const out = $("#result");
  setBusy(true);
  const started = Date.now();
  const show = () => out.innerHTML = `<div class="panel muted">Agent investigating ${esc(id)}… ${Math.round((Date.now() - started) / 1000)}s (up to ${Math.round(budget)}s). The evidence check above is already available. Duplicate requests are blocked while this runs.</div>`;
  show();
  const tick = setInterval(show, 500);
  try {
    const question = $("#question").value.trim() || null;
    const r = await post("/api/investigate", { robot_id: id, question });
    clearInterval(tick);
    renderResult(out, r);
    loadMemory(id);
    loadRobots();
  } catch (e) {
    clearInterval(tick);
    out.innerHTML = `<div class="panel err"><strong>Investigation failed:</strong> ${esc(e.message)}. No decision was recorded.</div>`;
  } finally {
    clearInterval(tick);
    setBusy(false);
  }
}

function lifecycle(d) {
  if (d.status === "rejected") return '<ol class="life"><li class="done">Recorded</li><li class="bad">Rejected by operator</li></ol>';
  const at = d.outcome ? 3 : { recorded: 0, approved: 1, executed: 2 }[d.status] ?? 0;
  const names = ["Recorded", "Approved", "Executed (reported by operator)", d.outcome ? "Outcome: " + words(d.outcome) : "Outcome"];
  return `<ol class="life">${names.map((n, i) => `<li class="${i <= at ? "done" : ""}">${esc(n)}</li>`).join("")}</ol>`;
}

function controls(d) {
  const btn = (act, text) => `<button class="secondary" data-act="${act}">${text}</button>`;
  if (d.status === "recorded") return `<div class="row top">${btn("approve", "Approve")}${btn("reject", "Reject")}<span class="muted small">Your decision. Approving does not move the robot.</span></div>`;
  if (d.status === "approved" && !d.outcome) return `<div class="row top">${btn("executed", "Mark as executed")}<span class="muted small">Use this after you carried the action out yourself.</span></div>`;
  if (d.status === "executed" && !d.outcome) return `<div class="row top"><span>Did it work?</span>${btn("recovered", "Recovered")}${btn("not_recovered", "Not recovered")}</div>`;
  return "";
}

function bindControls(d) {
  $("#lifecycle").innerHTML = lifecycle(d);
  const box = $("#controls");
  box.innerHTML = controls(d);
  box.querySelectorAll("button").forEach(b => b.onclick = async () => {
    const act = b.dataset.act;
    try {
      if (act === "approve" || act === "reject") d.status = (await post(`/api/decisions/${d.id}/approval`, { approve: act === "approve" })).status;
      else if (act === "executed") d.status = (await post(`/api/decisions/${d.id}/executed`)).status;
      else d.outcome = (await post(`/api/decisions/${d.id}/outcome`, { result: act })).outcome;
      bindControls(d);
      loadMemory(selected);
      loadAssessment(selected);
    } catch (e) {
      box.insertAdjacentHTML("beforeend", `<span class="err">${esc(e.message)}</span>`);
    }
  });
}

function agentPanel(r) {
  const t = r.timing, done = r.agent_completed;
  const state = done ? badge("supported", "completed") : badge("rejected", r.status === "fallback" ? "did not complete" : "failed");
  const body = done
    ? `<p><strong>Agent explanation</strong> <span class="muted small">(written by the model; the recorded decision below is what the graph accepted)</span></p><p>${esc(r.answer)}</p>`
    : `<p class="err"><strong>The agent did not record a decision:</strong> ${esc(r.reason)}.</p>${r.answer ? `<p class="muted small">Last model message, not a decision and not verified against the graph: ${esc(r.answer)}</p>` : ""}`;
  return `<div class="panel"><h2>AI agent result ${state}</h2>${body}
    <p class="muted small">${esc((t.total_ms / 1000).toFixed(1))}s total · model ${esc((t.model_ms / 1000).toFixed(1))}s over ${r.model_calls.length} call(s) · graph tools ${esc(t.tools_ms)}ms</p>
    <details><summary>Calls and timings</summary>
      <ol class="calls mono">${r.model_calls.map((m, i) => `<li>model call ${i + 1}: ${esc((m.ms / 1000).toFixed(1))}s, prompt ${esc(m.prompt_chars)} chars, ${esc(m.tool_calls)} tool call(s)</li>`).join("")}
      ${r.tool_calls.map(c => `<li class="${c.ok ? "" : "bad"}">${esc(c.tool)}(${esc(Object.values(c.args || {}).filter(v => typeof v !== "object").join(", "))}) <span class="muted">${c.prefetched ? "[prefetched] " : ""}${c.cached ? "[cached] " : ""}${esc(c.summary)} · ${esc(c.ms)}ms</span></li>`).join("")}</ol></details></div>`;
}

function decisionPanel(r) {
  const d = r.decision;
  if (!d) return `<div class="panel rec bad"><h2>Recorded decision</h2><p class="err"><strong>No decision was recorded.</strong> ${esc(r.reason)}.</p><p class="muted small">The evidence engine recommendation is only a proposal.</p></div>`;
  const by = r.recorded_by === "engine_fallback"
    ? '<p class="warn small">Recorded by the backend from the evidence engine, because the agent did not complete.</p>'
    : '<p class="muted small">Recorded from the agent\'s tool call after the graph validated it.</p>';
  return `<div class="panel rec"><h2>Recorded decision</h2><div class="action">${esc(label(d))} <span class="mono muted">${esc(d.id)}</span></div>${by}
    <div id="lifecycle"></div><div id="controls"></div>
    <p class="muted small">Advisory only. RoboGraph does not control robots: approval, execution and outcome are reported by the operator.</p></div>`;
}

function renderResult(out, r) {
  const steps = r.trace ? r.trace.steps : [];
  out.innerHTML = `
    <div class="grid">${agentPanel(r)}${decisionPanel(r)}</div>
    <div class="grid">
      <div class="panel"><h2>Decision trace</h2>
        <ol class="trace">${steps.map(s => `<li class="${esc(s.kind)}"><div class="t">${esc(s.title)}</div><div class="d">${esc(s.detail)}</div></li>`).join("") || '<li class="muted">No decision, so no trace.</li>'}</ol>
      </div>
      <div class="panel"><h2>Graph evidence</h2><div class="svgwrap" id="graph">${r.trace ? "" : '<p class="muted">No decision recorded.</p>'}</div></div>
    </div>`;
  if (r.trace) drawGraph($("#graph"), r.trace.graph);
  if (r.decision) bindControls(r.decision);
  if (r.assessment) $("#assessment").innerHTML = "<h2>Evidence engine recommendation</h2>" + renderAssessment({ ...r.assessment, cypher: r.cypher });
}

const listOf = (items, cls, title) => items && items.length ? `<div class="${cls}"><span class="lbl">${title}</span><ul class="ev">${items.map(t => `<li>${esc(t)}</li>`).join("")}</ul></div>` : "";

function cfColumn(title, a, changed) {
  const rows = a.options.map(o => {
    const c = changed.get(o.target || o.action);
    return `<div class="cfopt ${c ? "chg" : ""}"><strong>${esc(label(o))}</strong> ${badge(o.verdict)}<div class="muted small">${esc(o.reason)}</div></div>`;
  }).join("");
  return `<div class="panel"><h2>${esc(title)}</h2><p><strong>${esc(label(a.recommended))}</strong></p>${rows}</div>`;
}

function renderCounterfactual(r) {
  const tone = { unchanged: "same", changed: "changed", uncertain: "changed", failed: "failed" }[r.outcome];
  const head = { unchanged: "Recommendation unchanged", changed: "Recommendation changed", uncertain: "Recommendation became uncertain", failed: "Simulation failed" }[r.outcome];
  const check = r.original_unchanged ? '<span class="muted small">Original graph verified unchanged (node and relationship counts); the copy was deleted.</span>' : '<span class="err small">Original graph counts differ after the run (another write may have happened). Check the graph.</span>';
  if (!r.counterfactual) return `<p class="banner failed"><strong>${head}.</strong> ${esc(r.explanation)} ${esc(r.simulation.error || "")}</p>${check}`;
  const changed = new Map(r.option_changes.map(c => [c.target || c.action, c]));
  const detail = r.option_changes.map(c => `<div class="cfchange"><strong>${esc(c.target || words(c.action))}</strong>: ${c.verdict_changed ? `${badge(c.before)} → ${badge(c.after)}` : `still ${badge(c.after)}, evidence changed`}
    ${listOf(c.support_removed, "rem", "Support removed")}${listOf(c.support_added, "add", "Support added")}${listOf(c.contradiction_added, "rem", "Contradiction added")}${listOf(c.contradiction_removed, "add", "Contradiction removed")}</div>`).join("");
  let agent = "";
  if (r.agent) {
    agent = r.agent.decision
      ? `<p class="banner">Agent on the modified copy (${esc((r.agent.elapsed_ms / 1000).toFixed(1))}s): <strong>${esc(label(r.agent.decision))}</strong> ${r.agent.agrees_with_evidence ? badge("supported", "matches evidence engine") : badge("rejected", "differs from evidence engine")} <span class="muted small">Not saved.</span></p>`
      : `<p class="banner failed">The agent did not reach a valid decision on the modified copy: ${esc(r.agent.reason)}.</p>`;
  }
  return `<div class="grid">${cfColumn("Original graph", r.baseline, changed)}${cfColumn("After: " + r.change.description, r.counterfactual, changed)}</div>
    <p class="banner ${tone}"><strong>${head}.</strong> ${esc(r.explanation)}</p>
    ${detail ? `<div class="panel"><h2>What changed</h2>${detail}</div>` : ""}${agent}${check}`;
}

async function counterfactual(id) {
  if (busy) return;
  const out = $("#cfout");
  setBusy(true);
  out.innerHTML = `<p class="muted">Running on a copy of the graph${$("#cfagent").checked ? ", then the agent (up to " + Math.round(budget) + "s)" : ""}…</p>`;
  try {
    out.innerHTML = renderCounterfactual(await post("/api/counterfactual", { robot_id: id, change: $("#cfchange").value, location_id: $("#cfloc").value, use_agent: $("#cfagent").checked }));
  } catch (e) {
    out.innerHTML = `<p class="err">${esc(e.message)}</p>`;
  } finally {
    setBusy(false);
  }
}

async function evaluation() {
  selected = null;
  document.querySelectorAll("#robots li").forEach(li => li.classList.remove("sel"));
  const box = $("#detail");
  box.innerHTML = '<div class="panel muted">Running scenarios on isolated copies of the graph…</div>';
  try {
    const r = await post("/api/evaluate");
    box.innerHTML = `<div class="panel"><h2>Evaluation: ${esc(r.passed)} of ${esc(r.total)} passed</h2><p class="muted small">${esc(r.note)}</p>
      <table><tr><th>scenario</th><th>expected</th><th>actual</th><th></th></tr>
      ${r.results.map(x => `<tr><td><strong>${esc(x.name)}</strong><br><span class="muted small">${esc(x.why)}</span></td><td>${esc(x.expected)}</td><td>${esc(x.actual)}</td><td class="${x.passed ? "pass" : "fail"}">${x.passed ? "pass" : "fail"}</td></tr>`).join("")}</table></div>`;
  } catch (e) {
    box.innerHTML = `<p class="err">${esc(e.message)}</p>`;
  }
}

function drawGraph(host, g) {
  const nodes = g.nodes.filter(n => n.kind !== "engineer");
  const edges = g.edges.filter(e => e.type !== "SUPPORTED_BY" && e.type !== "RESOLVED_BY");
  const focus = (edges.find(e => e.type === "ABOUT") || {}).target;
  const decision = nodes.find(n => n.kind === "decision");
  const located = (edges.find(e => e.type === "LOCATED_AT" && e.source === focus) || {}).target;
  const target = (edges.find(e => e.type === "RECOMMENDS") || {}).target;
  const rank = n => (n.id === located ? 0 : n.id === target ? 1 : 2);
  const columns = [
    nodes.filter(n => n.kind === "robot" && n.id !== focus),
    nodes.filter(n => n.kind === "incident"),
    nodes.filter(n => n.kind === "location").sort((a, b) => rank(a) - rank(b)),
    [nodes.find(n => n.id === focus), decision].filter(Boolean),
  ];
  const W = 96, H = 26, X = [8, 188, 368, 548], GAP = 52;
  const rows = Math.max(...columns.map(c => c.length), 1);
  const height = rows * GAP + 12, width = X[3] + W + 8;
  const pos = {};
  columns.forEach((col, c) => col.forEach((n, i) => { pos[n.id] = { x: X[c], y: 8 + ((rows - col.length) * GAP) / 2 + i * GAP, c }; }));
  const NS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.setAttribute("width", width);
  const add = (parent, tag, attrs, text) => {
    const el = document.createElementNS(NS, tag);
    Object.entries(attrs).forEach(([k, v]) => el.setAttribute(k, v));
    if (text !== undefined) el.textContent = text;
    parent.appendChild(el);
    return el;
  };
  const labels = [];
  edges.forEach(e => {
    const a = pos[e.source], b = pos[e.target];
    if (!a || !b) return;
    const same = a.c === b.c, fwd = b.c > a.c, down = b.y > a.y;
    const ax = same ? a.x + W / 2 : fwd ? a.x + W : a.x, bx = same ? b.x + W / 2 : fwd ? b.x : b.x + W;
    const ay = a.y + H / 2 + (same ? (down ? H / 2 : -H / 2) : 0), by = b.y + H / 2 + (same ? (down ? -H / 2 : H / 2) : 0);
    add(svg, "line", { x1: ax, y1: ay, x2: bx, y2: by, class: `edge ${e.type}` });
    const t = same ? 0.5 : 0.3;
    labels.push({ x: ax + (bx - ax) * t, y: ay + (by - ay) * t, text: e.type, cls: e.type });
  });
  nodes.forEach(n => {
    const p = pos[n.id];
    if (!p) return;
    const grp = add(svg, "g", { class: n.kind });
    add(grp, "rect", { x: p.x, y: p.y, width: W, height: H, rx: 2, class: "n" });
    add(grp, "text", { x: p.x + W / 2, y: p.y + H / 2, class: "nl" }, n.label.length > 14 ? n.label.slice(0, 13) + "…" : n.label);
  });
  labels.forEach(l => {
    const w = l.text.length * 5.4 + 6;
    add(svg, "rect", { x: l.x - w / 2, y: l.y - 6, width: w, height: 12, class: "lbg" });
    add(svg, "text", { x: l.x, y: l.y, class: `elabel ${l.cls}` }, l.text);
  });
  host.replaceChildren(svg);
}

$("#eval").onclick = evaluation;
loadHealth();
loadRobots();
