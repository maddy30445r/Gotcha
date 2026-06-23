"use strict";

/* ── config: backend URL + auth token + native bridge ────────────────── */
const TAURI = !!window.__TAURI__;
const invoke = TAURI ? window.__TAURI__.core.invoke : null;
const DEFAULT_SERVER = "http://localhost:8000";
const serverUrl = () => (localStorage.getItem("gotcha_server") || DEFAULT_SERVER).replace(/\/+$/, "");
const authToken = () => localStorage.getItem("gotcha_token") || "";

/* ── state ───────────────────────────────────────────────────────────── */
let current = null;       // { base, transcript, tracks, your_name }
let recSession = null;    // { base, system_path, mic_path } from native start
let recording = false;
let timerId = null;
let pollId = null;
let trackMode = "mix";    // mix | mic (You) | system (Them)
let lastClip = null;      // { ts, chipEl }
const PROCESSING = new Set(["queued", "recording", "transcribing", "interpreting"]);

const $ = (s) => document.querySelector(s);
const listEl = $("#meeting-list");
const reportEl = $("#report");
const recBtn = $("#rec-btn");
const recTimer = $("#rec-timer");
const recName = $("#rec-name");
const recText = recBtn.querySelector(".rec-text");
const player = $("#player");
const playerLabel = $("#player-label");
const playerEq = $("#player-eq");

/* ── tiny helpers ────────────────────────────────────────────────────── */
async function api(path, opts) {
  opts = opts || {};
  const headers = Object.assign({}, opts.headers);
  const tok = authToken();
  if (tok) headers["Authorization"] = "Bearer " + tok;
  const res = await fetch(serverUrl() + path, Object.assign({}, opts, { headers }));
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = (await res.json()).detail || msg; } catch (_) {}
    throw new Error(msg);
  }
  return res.status === 204 ? null : res.json();
}
const esc = (s) => String(s).replace(/[&<>"]/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const fmtTime = (s) => {
  s = Math.max(0, Math.floor(s));
  return String((s / 60) | 0).padStart(2, "0") + ":" + String(s % 60).padStart(2, "0");
};
const pillHTML = (ts) =>
  `<span class="pill" data-ts="${ts}"><span class="pill-rec"></span>${ts}s<span class="pill-play">▶</span></span>`;
// escape → bold → turn [Ns] citations into receipt pills
function mdInline(str) {
  let h = esc(str).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>");
  return h.replace(/\[(\d+(?:\.\d+)?)\s*s\]/g, (_, ts) => pillHTML(ts));
}
const stripStars = (s) => s.replace(/^\s*\*\*|\*\*\s*$/g, "").trim();
const tsIn = (s) => { const m = s.match(/\[(\d+(?:\.\d+)?)\s*s\]/); return m ? m[1] : null; };

/* ── report parser: report_md → structured cards ─────────────────────── */
function topBullets(body) {
  const items = []; let cur = null;
  for (const ln of body.split("\n")) {
    if (/^[*-]\s+/.test(ln)) { cur = { head: ln.replace(/^[*-]\s+/, ""), subs: [] }; items.push(cur); }
    else if (/^\s+[*-]\s+/.test(ln) && cur) { cur.subs.push(ln.replace(/^\s+[*-]\s+/, "")); }
    else if (cur && ln.trim()) {
      if (cur.subs.length) cur.subs[cur.subs.length - 1] += " " + ln.trim();
      else cur.head += " " + ln.trim();
    }
  }
  return items;
}

function renderSummary(body) {
  const paras = body.trim().split(/\n\s*\n/).map((p) => `<p>${mdInline(p.trim())}</p>`).join("");
  return `<div class="summary-prose">${paras}</div>`;
}

function renderDecode(body) {
  const items = topBullets(body);
  if (!items.length) {  // "Nothing implicit — the lead was direct throughout."
    return `<p class="decode-none">${esc(body.trim())}</p>`;
  }
  return items.map((it) => {
    const ts = tsIn(it.head);
    let quote = it.head.replace(/\bat\s*\[[^\]]*\]/i, "").replace(/\[[^\]]*\]/, "");
    quote = stripStars(quote).replace(/^[“"']|[”"']$/g, "").trim();
    let meaning = "";
    for (const s of it.subs) {
      const m = s.match(/^\*\*\s*What.*?meant:?\s*\*\*\s*(.*)/i);
      if (m) { meaning = m[1]; break; }
    }
    if (!meaning && it.subs.length) meaning = it.subs.join(" ").replace(/^\*\*.*?:\*\*\s*/, "");
    return `<div class="decode-card">
        <p class="dc-tag">what your lead said</p>
        <div class="dc-said"><q>${esc(quote)}</q>${ts ? pillHTML(ts) : ""}</div>
        <p class="dc-arrow">↓ what they really meant</p>
        <p class="dc-meant">${mdInline(meaning)}</p>
      </div>`;
  }).join("");
}

function renderActions(body) {
  const items = topBullets(body);
  if (!items.length) return `<p class="decode-none">${esc(body.trim())}</p>`;
  const PCLASS = { high: "prio--high", medium: "prio--med", low: "prio--low" };
  return items.map((it) => {
    let task = stripStars(it.head), verify = "";
    const vm = task.match(/\((verify[^)]*)\)/i);
    if (vm) { verify = vm[1]; task = task.replace(vm[0], "").trim(); }

    let level = "", why = "", source = "";
    for (const s of it.subs) {
      let m;
      if ((m = s.match(/^\*\*\s*Priority:?\s*\*\*\s*(.*)/i))) level = m[1].trim();
      else if ((m = s.match(/^\*\*\s*Why:?\s*\*\*\s*(.*)/i))) why = m[1].trim();
      else if ((m = s.match(/^\*\*\s*Source:?\s*\*\*\s*(.*)/i))) source = m[1].trim();
    }
    const lvlWord = (level.match(/^(High|Medium|Low)/i) || [, ""])[1];
    const pclass = PCLASS[lvlWord.toLowerCase()] || "prio--med";
    const ts = tsIn(source);
    let quote = source.replace(/\[[^\]]*\]/, "").trim().replace(/^[“"']|[”"']$/g, "");

    return `<div class="action" data-prio="${esc(lvlWord || "Medium")}">
        <span class="act-box"></span>
        <div class="act-main">
          <div class="act-row">
            <span class="act-task">${esc(task)}</span>
            ${lvlWord ? `<span class="prio ${pclass}" title="${esc(level)}">${esc(lvlWord)}</span>` : ""}
            ${verify ? `<span class="act-verify">${esc(verify)}</span>` : ""}
          </div>
          ${why ? `<p class="act-why">${mdInline(why)}</p>` : ""}
          ${ts ? `<div class="act-source">${pillHTML(ts)}<span class="act-quote">“${esc(quote)}”</span></div>` : ""}
        </div>
      </div>`;
  }).join("");
}

const EYEBROW = { summary: "the gist", decode: "reading between the lines", actions: "your move" };
function classify(title, idx) {
  const t = title.toLowerCase();
  if (/really meant|lead/.test(t)) return "decode";
  if (/action item|your task|to.?do/.test(t)) return "actions";
  if (/summary|decision/.test(t)) return "summary";
  return ["summary", "decode", "actions"][idx] || "summary";
}

function renderReport(md) {
  const norm = md.replace(/\r\n/g, "\n");
  const parts = norm.split(/^###\s+/m);
  const preamble = parts.shift().trim();
  let html = "";
  if (preamble) html += `<p class="report-greeting">${mdInline(preamble)}</p>`;

  parts.forEach((chunk, i) => {
    const nl = chunk.indexOf("\n");
    const rawTitle = (nl === -1 ? chunk : chunk.slice(0, nl)).replace(/^\d+\.\s*/, "").trim();
    const body = nl === -1 ? "" : chunk.slice(nl + 1);
    const kind = classify(rawTitle, i);
    const inner = kind === "decode" ? renderDecode(body)
      : kind === "actions" ? renderActions(body)
      : renderSummary(body);
    html += `<section class="section reveal">
        <p class="section-eyebrow">${EYEBROW[kind]}</p>
        <h3 class="section-title">${esc(rawTitle)}</h3>
        ${inner}
      </section>`;
  });
  return html;
}

/* ── meetings list ───────────────────────────────────────────────────── */
function badge(state, hasReport) {
  if (state === "error") return '<span class="badge error">failed</span>';
  if (PROCESSING.has(state)) return `<span class="badge processing">${state}</span>`;
  if (hasReport) return '<span class="badge done">ready</span>';
  return "";
}

async function loadMeetings(autoOpen) {
  let data;
  try { data = await api("/api/meetings"); } catch (_) { return; }
  listEl.innerHTML = "";
  for (const m of data.meetings) {
    const li = document.createElement("li");
    if (current && current.base === m.base) li.classList.add("active");
    li.dataset.base = m.base;
    li.innerHTML =
      `<div class="mi-name">${esc(m.base)}</div>` +
      `<div class="mi-meta">${badge(m.state, m.has_report)}` +
      `${Object.keys(m.tracks).length ? '<span class="badge audio">audio</span>' : ""}</div>`;
    li.onclick = () => openMeeting(m.base);
    listEl.appendChild(li);
  }
  if (autoOpen && !current && data.meetings[0]) openMeeting(data.meetings[0].base, true);
}

async function openMeeting(base, quiet) {
  let m;
  try { m = await api("/api/meetings/" + encodeURIComponent(base)); }
  catch (e) { reportEl.innerHTML = `<div class="status-line error">${esc(e.message)}</div>`; return; }

  current = { base, transcript: m.transcript || [], tracks: m.tracks || {}, your_name: m.your_name };
  [...listEl.children].forEach((li) => li.classList.toggle("active", li.dataset.base === base));

  let html = "";
  if (m.state === "error") html += `<div class="status-line error">Couldn’t finish this one: ${esc(m.error || "unknown error")}</div>`;
  else if (PROCESSING.has(m.state)) html += `<div class="status-line">Working on it — ${esc(m.state)}. This takes a few minutes.</div>`;

  if (m.report_md) {
    try { html += renderReport(m.report_md); }
    catch (_) { html += `<div class="report-fallback">${m.report_html || ""}</div>`; }
  } else if (m.report_html) {
    html += `<div class="report-fallback">${m.report_html}</div>`;
  } else if (!PROCESSING.has(m.state) && m.state !== "error") {
    html += `<p class="report-empty">No report yet for this meeting.</p>`;
  }
  reportEl.innerHTML = html;
  revealIn(reportEl);

  if (PROCESSING.has(m.state)) pollMeeting(base);
  if (!quiet) document.getElementById("workspace").scrollIntoView({ behavior: "smooth", block: "start" });
}

function pollMeeting(base) {
  if (pollId) clearInterval(pollId);
  pollId = setInterval(async () => {
    let j; try { j = await api("/api/jobs/" + encodeURIComponent(base)); } catch (_) { return; }
    if (!PROCESSING.has(j.state)) {
      clearInterval(pollId); pollId = null;
      await loadMeetings();
      if (current && current.base === base) openMeeting(base, true);
    }
  }, 3000);
}

/* ── citation → audio (the receipt, playing) ─────────────────────────── */
function resolveTrack(mode) {
  const t = current.tracks || {};
  if (mode === "mic" && t.mic) return "mic";
  if (mode === "system" && t.system) return "system";
  if (mode === "mix" && t.mix) return "mix";
  return t.mix ? "mix" : t.single ? "single" : t.system ? "system" : t.mic ? "mic" : null;
}

function seekTo(ts, chip) {
  if (!current) return;
  lastClip = { ts, chip };
  const track = resolveTrack(trackMode);
  if (!track) { playerLabel.textContent = "No audio was recorded for this meeting."; return; }

  document.querySelectorAll(".pill.playing").forEach((c) => c.classList.remove("playing"));
  if (chip) chip.classList.add("playing");
  playerLabel.textContent = `${current.base} · ${track} · ${(+ts).toFixed(1)}s`;

  const src = `${serverUrl()}/api/audio/${encodeURIComponent(current.base)}/${track}`
    + `?token=${encodeURIComponent(authToken())}`;
  const go = () => { try { player.currentTime = +ts; } catch (_) {} player.play(); };
  if (player.dataset.src !== src) {
    player.dataset.src = src; player.src = src;
    player.addEventListener("loadedmetadata", go, { once: true });
    player.load();
  } else go();
}

reportEl.addEventListener("click", (ev) => {
  const chip = ev.target.closest(".pill, .cite");
  if (!chip || !current) return;
  const ts = parseFloat(chip.dataset.ts);
  if (!isNaN(ts)) seekTo(ts, chip);
});

document.querySelectorAll("#track-seg button").forEach((btn) => {
  btn.onclick = () => {
    document.querySelectorAll("#track-seg button").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    trackMode = btn.dataset.mode;
    if (lastClip) seekTo(lastClip.ts, lastClip.chip);
  };
});

player.addEventListener("play", () => playerEq.classList.add("on"));
player.addEventListener("pause", () => playerEq.classList.remove("on"));
player.addEventListener("ended", () => {
  playerEq.classList.remove("on");
  document.querySelectorAll(".pill.playing").forEach((c) => c.classList.remove("playing"));
});

/* ── record control (native capture via Tauri → upload) ──────────────── */
async function startRecording() {
  if (!TAURI) { alert("Recording needs the Gotcha desktop app."); return; }
  if (!authToken()) { openSettings(); return; }
  recBtn.disabled = true;
  try {
    recSession = await invoke("start_recording", { name: recName.value || "meeting" });
  } catch (e) {
    const msg = String(e);
    if (/[Pp]ermission/.test(msg)) openPerm();   // guide the grant instead of a dead-end alert
    else alert("Couldn’t start recording:\n" + msg);
    recBtn.disabled = false; return;
  }
  recording = true;
  recText.textContent = "Stop";
  recBtn.classList.add("recording");
  recBtn.disabled = false; recName.disabled = true;
  recTimer.hidden = false;
  let secs = 0; recTimer.textContent = "00:00";
  timerId = setInterval(() => { secs++; recTimer.textContent = fmtTime(secs); }, 1000);
}

async function stopRecording() {
  recBtn.disabled = true;
  if (timerId) { clearInterval(timerId); timerId = null; }
  let sess;
  try { sess = await invoke("stop_recording"); }
  catch (e) { alert("Stop failed:\n" + e); resetRecUI(); return; }
  // Capture is done locally; now upload the two tracks (the paid step on the server).
  recText.textContent = "Uploading…";
  let serverBase;
  try {
    serverBase = await invoke("upload_recording", {
      serverUrl: serverUrl(), token: authToken(),
      name: recName.value || "meeting",
      systemPath: sess.system_path, micPath: sess.mic_path,
    });
  } catch (e) { alert("Upload failed:\n" + e); resetRecUI(); return; }
  resetRecUI();
  recSession = null;
  await loadMeetings();
  if (serverBase) openMeeting(serverBase);
}

function resetRecUI() {
  recording = false;
  if (timerId) { clearInterval(timerId); timerId = null; }
  recText.textContent = "Record";
  recBtn.classList.remove("recording");
  recBtn.disabled = false; recName.disabled = false;
  recTimer.hidden = true;
}
recBtn.onclick = () => (recording ? stopRecording() : startRecording());

/* ── settings (server URL + token, stored locally) ───────────────────── */
function openSettings() {
  const dlg = $("#settings");
  $("#set-server").value = serverUrl();
  $("#set-token").value = authToken();
  dlg.showModal ? dlg.showModal() : (dlg.hidden = false);
}
function saveSettings(ev) {
  ev.preventDefault();
  localStorage.setItem("gotcha_server", ($("#set-server").value.trim() || DEFAULT_SERVER));
  localStorage.setItem("gotcha_token", $("#set-token").value.trim());
  const dlg = $("#settings");
  dlg.close ? dlg.close() : (dlg.hidden = true);
  loadMeetings(true);
}
$("#settings-btn").onclick = openSettings;
$("#settings-form").addEventListener("submit", saveSettings);

/* ── zero-paste onboarding: gotcha://connect?server=&token= ──────────── */
function applyConnectUrl(raw) {
  try {
    const u = new URL(raw);
    const server = u.searchParams.get("server");
    const token = u.searchParams.get("token");
    if (server) localStorage.setItem("gotcha_server", server.replace(/\/+$/, ""));
    if (token) localStorage.setItem("gotcha_token", token);
    const dlg = $("#settings"); if (dlg && dlg.open) dlg.close();
    loadMeetings(true);
    return true;
  } catch (_) { return false; }
}
if (TAURI && window.__TAURI__.event) {
  // The site's "Open in Gotcha" link routes here via the Rust deep-link handler.
  window.__TAURI__.event.listen("deep-link", (e) => applyConnectUrl(e.payload));
}

/* ── permission wizard (opened when capture is blocked) ──────────────── */
function openPerm() {
  const dlg = $("#perm");
  dlg.showModal ? dlg.showModal() : (dlg.hidden = false);
}
document.querySelectorAll("#perm [data-pane]").forEach((b) => {
  b.onclick = () => { if (invoke) invoke("open_privacy_pane", { which: b.dataset.pane }); };
});
const permClose = $("#perm-close");
if (permClose) permClose.onclick = () => { const d = $("#perm"); d.close ? d.close() : (d.hidden = true); };
const permRelaunch = $("#perm-relaunch");
if (permRelaunch) permRelaunch.onclick = () => { if (invoke) invoke("relaunch"); };

// Build a shareable connect link from the current settings (for an admin to send).
const copyBtn = $("#copy-link");
if (copyBtn) copyBtn.onclick = () => {
  const server = ($("#set-server").value.trim() || DEFAULT_SERVER).replace(/\/+$/, "");
  const token = $("#set-token").value.trim();
  const link = `gotcha://connect?server=${encodeURIComponent(server)}&token=${encodeURIComponent(token)}`;
  if (navigator.clipboard) navigator.clipboard.writeText(link);
  copyBtn.textContent = "Copied ✓";
  setTimeout(() => (copyBtn.textContent = "Copy connect link"), 1500);
};

/* ── motion: one orchestrated reveal ─────────────────────────────────── */
function revealIn(scope) {
  const els = (scope || document).querySelectorAll(".reveal:not(.in)");
  els.forEach((el, i) => setTimeout(() => el.classList.add("in"), 60 + i * 70));
}

$("#hero-cta").onclick = () => {
  document.getElementById("workspace").scrollIntoView({ behavior: "smooth", block: "start" });
  if (!current) loadMeetings(true);
};

/* ── boot ────────────────────────────────────────────────────────────── */
revealIn(document);             // hero demo fades up
if (!authToken()) openSettings();   // first run: ask for backend URL + token
loadMeetings(true);             // populate sidebar + auto-open newest into the workspace
setInterval(() => loadMeetings(false), 8000);
