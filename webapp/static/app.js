"use strict";

/* ── config: backend URL + auth token + native bridge ────────────────── */
const TAURI = !!window.__TAURI__;
const invoke = TAURI ? window.__TAURI__.core.invoke : null;
const DEFAULT_SERVER = "https://gotcha-trial.duckdns.org";
const DEFAULT_REC_NAME = "meeting";
// Web: the app is served BY the backend, so it's same-origin — use relative paths
// and authenticate by the session cookie. Desktop: a different origin (tauri://),
// so it targets the stored server URL and authenticates by bearer token.
const serverUrl = () =>
  TAURI ? (localStorage.getItem("gotcha_server") || DEFAULT_SERVER).replace(/\/+$/, "") : "";
const authToken = () => (TAURI ? localStorage.getItem("gotcha_token") || "" : "");

/* ── state ───────────────────────────────────────────────────────────── */
let current = null;       // { base, transcript, tracks, your_name, name }
let recSession = null;    // { base, system_path, mic_path } from native start
let pendingRec = null;    // captured-but-not-yet-uploaded session
let recording = false;
let timerId = null;
let pollId = null;
let trackMode = "mix";    // mix | mic (You) | system (Them)
let lastClip = null;      // { ts, chip }
let waveBars = { you: [], them: [] };
const meetingDur = {};    // base → audio duration (s), filled lazily from <audio> metadata
const NB = 80;            // bars per waveform row
const PROCESSING = new Set(["queued", "recording", "transcribing", "interpreting"]);

const $ = (s) => document.querySelector(s);
const listEl = $("#meeting-list");
const reportEl = $("#report");
const recBtn = $("#rec-btn");
const recTimer = $("#rec-timer");
const recText = recBtn.querySelector(".rec-text");
const player = $("#player");
const playerBar = $("#player-bar");
const playerFile = $("#player-file");
const titlePill = $("#current-title");
const titleInput = $("#title-input");

/* ── toasts (bottom-center dark pill) ────────────────────────────────── */
function toast(msg, kind) {
  const wrap = document.getElementById("toasts");
  if (!wrap) { console.warn(msg); return; }
  const el = document.createElement("div");
  el.className = "toast" + (kind ? " " + kind : "");
  el.textContent = msg;
  wrap.appendChild(el);
  requestAnimationFrame(() => el.classList.add("in"));
  setTimeout(() => { el.classList.remove("in"); setTimeout(() => el.remove(), 220); }, 3200);
}

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
    const err = new Error(msg); err.status = res.status; throw err;
  }
  return res.status === 204 ? null : res.json();
}
const esc = (s) => String(s).replace(/[&<>"]/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const fmtTime = (s) => {
  s = Math.max(0, Math.floor(s || 0));
  return String((s / 60) | 0) + ":" + String(s % 60).padStart(2, "0");
};
const pillHTML = (ts) => `<span class="pill" data-ts="${ts}">${(+ts).toFixed(1)}s</span>`;
// escape → bold → turn [Ns] citations into receipt pills
function mdInline(str) {
  let h = esc(str).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>");
  return h.replace(/\[(\d+(?:\.\d+)?)\s*s?\]/g, (_, ts) => pillHTML(ts));
}
const stripStars = (s) => s.replace(/^\s*\*\*|\*\*\s*$/g, "").trim();
const tsIn = (s) => { const m = s.match(/\[(\d+(?:\.\d+)?)\s*s?\]/); return m ? m[1] : null; };

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
        <div class="dc-tag">THEY SAID</div>
        <div class="dc-said"><q>${esc(quote)}</q>${ts ? pillHTML(ts) : ""}</div>
        <div class="dc-arrow">↓ REALLY MEANS</div>
        <div class="dc-meant">${mdInline(meaning)}</div>
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
    const howLines = []; let collectingHow = false;
    for (const s of it.subs) {
      let m;
      if ((m = s.match(/^\*\*\s*Priority:?\s*\*\*\s*(.*)/i))) { level = m[1].trim(); collectingHow = false; }
      else if ((m = s.match(/^\*\*\s*Why:?\s*\*\*\s*(.*)/i))) { why = m[1].trim(); collectingHow = false; }
      else if ((m = s.match(/^\*\*\s*Source:?\s*\*\*\s*(.*)/i))) { source = m[1].trim(); collectingHow = false; }
      else if ((m = s.match(/^\*\*\s*How:?\s*\*\*\s*(.*)/i))) { collectingHow = true; if (m[1].trim()) howLines.push(m[1].trim()); }
      else if (collectingHow && s.trim()) { howLines.push(s.trim()); }
    }
    const howHTML = howLines.map((l) => mdInline(l).replace(/;\s+/g, "<br>")).join("<br>");
    const lvlWord = (level.match(/^(High|Medium|Low)/i) || [, ""])[1];
    const chipLabel = /^medium$/i.test(lvlWord) ? "Med" : lvlWord;
    const pclass = PCLASS[lvlWord.toLowerCase()] || "prio--med";
    const ts = tsIn(source);
    let quote = source.replace(/\[[^\]]*\]/, "").trim().replace(/^[“"']|[”"']$/g, "");

    return `<div class="action"${ts ? ` data-ts="${ts}"` : ""} data-prio="${esc(lvlWord || "Medium")}">
        <div class="action-inner">
          <div class="act-box" role="checkbox" aria-checked="false" tabindex="0"></div>
          <div class="act-main">
            <div class="act-row">
              <span class="act-task">${esc(task)}</span>
              ${lvlWord ? `<span class="prio ${pclass}" title="${esc(level)}">${esc(chipLabel)}</span>` : ""}
              ${verify ? `<span class="act-verify">${esc(verify)}</span>` : ""}
            </div>
            ${why ? `<p class="act-why">${mdInline(why)}</p>` : ""}
            ${ts ? `<div class="act-source">${pillHTML(ts)}${quote ? `<span class="act-quote">“${esc(quote)}”</span>` : ""}</div>` : ""}
            ${howHTML ? `<details class="act-how"><summary>HOW TO DO IT</summary><div class="act-how-body">${howHTML}</div></details>` : ""}
          </div>
        </div>
      </div>`;
  }).join("");
}

function renderQuestions(body) {
  const items = topBullets(body);
  if (!items.length) {  // "Nothing open — everything was clear."
    return `<p class="decode-none">${esc(body.trim())}</p>`;
  }
  return items.map((it) => {
    const ts = tsIn(it.head);
    let q = it.head.replace(/\bat\s*\[[^\]]*\]/i, "").replace(/\[[^\]]*\]/, "");
    q = stripStars(q).replace(/^[“"']|[”"']$/g, "").trim();
    return `<div class="q-card"><span class="q-text">${mdInline(q)} ${ts ? pillHTML(ts) : ""}</span></div>`;
  }).join("");
}

const EYEBROW = { summary: "the gist", decode: "reading between the lines", actions: "your move", questions: "before you act" };
function classify(title, idx) {
  const t = title.toLowerCase();
  if (/really meant|lead/.test(t)) return "decode";
  if (/open question|to verify|unresolved|to confirm/.test(t)) return "questions";
  if (/action item|your task|to.?do|your move/.test(t)) return "actions";
  if (/summary|decision/.test(t)) return "summary";
  return ["summary", "decode", "actions", "questions"][idx] || "summary";
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
    const tag = kind === "summary" ? "h1" : "h2";
    const wrapOpen = kind === "actions" ? '<div class="actions-wrap">' : kind === "questions" ? '<div class="questions-wrap">' : "";
    const wrapClose = wrapOpen ? "</div>" : "";
    const inner = kind === "decode" ? renderDecode(body)
      : kind === "actions" ? renderActions(body)
      : kind === "questions" ? renderQuestions(body)
      : renderSummary(body);
    html += `<section class="report-section kind-${kind} reveal">
        <div class="section-eyebrow">${EYEBROW[kind]}</div>
        <${tag} class="section-title">${esc(rawTitle)}</${tag}>
        ${wrapOpen}${inner}${wrapClose}
      </section>`;
  });
  return html;
}

/* ── meetings list ───────────────────────────────────────────────────── */
function badge(state, hasReport) {
  if (state === "error") return '<span class="badge error">failed</span>';
  if (state === "parked") return '<span class="badge parked">parked</span>';
  if (state === "recording") return '<span class="badge recording">recording</span>';
  if (PROCESSING.has(state)) return `<span class="badge processing">decoding</span>`;
  if (hasReport) return '<span class="badge done">ready</span>';
  return "";
}

let allMeetings = [];
function fmtDate(epoch) {
  if (!epoch) return "";
  const d = new Date(epoch * 1000), now = new Date();
  const yest = new Date(now); yest.setDate(now.getDate() - 1);
  if (d.toDateString() === now.toDateString()) return "Today";
  if (d.toDateString() === yest.toDateString()) return "Yesterday";
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function renderMeetingList() {
  const q = (($("#meeting-search") || {}).value || "").trim().toLowerCase();
  const shown = q
    ? allMeetings.filter((m) => (m.name || m.base).toLowerCase().includes(q))
    : allMeetings;
  listEl.innerHTML = "";
  if (!shown.length) {
    const li = document.createElement("li");
    li.className = "list-empty";
    li.textContent = q ? `No meetings match “${q}”.` : "No meetings yet — hit Record.";
    listEl.appendChild(li);
    return;
  }
  for (const m of shown) {
    const dur = meetingDur[m.base] ? fmtTime(meetingDur[m.base]) : "—";
    const li = document.createElement("li");
    if (current && current.base === m.base) li.classList.add("active");
    li.dataset.base = m.base;
    li.innerHTML =
      `<div class="mi-top"><span class="mi-name">${esc(m.name || m.base)}</span>${badge(m.state, m.has_report)}</div>` +
      `<div class="mi-meta">${esc(fmtDate(m.created))} · ${esc(dur)}</div>` +
      `<button class="mi-del" title="Delete meeting" aria-label="Delete meeting">✕</button>`;
    li.onclick = () => openMeeting(m.base);
    li.querySelector(".mi-del").onclick = (e) => { e.stopPropagation(); deleteMeeting(m.base); };
    listEl.appendChild(li);
  }
}

async function loadMeetings(autoOpen) {
  let data;
  try { data = await api("/api/meetings"); } catch (_) { return; }
  allMeetings = data.meetings || [];
  renderMeetingList();
  if (autoOpen && !current && allMeetings[0]) openMeeting(allMeetings[0].base, true);
}

/* ── rename (inline in the header pill) + export ─────────────────────── */
function startRename() {
  if (!current) return;
  const name = (allMeetings.find((x) => x.base === current.base) || {}).name || current.name || current.base;
  titlePill.hidden = true;
  titleInput.hidden = false;
  titleInput.value = name;
  titleInput.focus();
  titleInput.select();
}
function cancelRename() { titleInput.hidden = true; titlePill.hidden = false; }
async function commitRename() {
  if (titleInput.hidden) return;
  const name = (titleInput.value || "").trim();
  cancelRename();
  if (!name || !current) return;
  if (name === titlePill.textContent) return;
  try {
    await api("/api/meetings/" + encodeURIComponent(current.base), {
      method: "PATCH", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
  } catch (e) { toast("Couldn't rename: " + e.message, "err"); return; }
  setTitle(name);
  toast("Renamed", "ok");
  await loadMeetings();
}
function setTitle(name) {
  if (name) { titlePill.textContent = name; titlePill.classList.remove("empty"); }
  else { titlePill.textContent = "No meeting selected"; titlePill.classList.add("empty"); }
}
titlePill.onclick = () => { if (current) startRename(); };
titleInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter") commitRename();
  else if (e.key === "Escape") cancelRename();
});
titleInput.addEventListener("blur", commitRename);

function exportReport(base, mdText) {
  if (!mdText) { toast("No report to export yet.", "err"); return; }
  const m = allMeetings.find((x) => x.base === base);
  const fname = ((m && m.name) || base).replace(/[^\w-]+/g, "_") + ".md";
  const blob = new Blob([mdText], { type: "text/markdown" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob); a.download = fname;
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  toast(`Exported “${(m && m.name) || base}” decode to Markdown.`, "ok");
}

/* ── open a meeting ──────────────────────────────────────────────────── */
async function openMeeting(base, quiet) {
  let m;
  try { m = await api("/api/meetings/" + encodeURIComponent(base)); }
  catch (e) { reportEl.innerHTML = `<div class="status-line error">${esc(e.message)}</div>`; return; }

  const meta = allMeetings.find((x) => x.base === base) || {};
  current = { base, transcript: m.transcript || [], tracks: m.tracks || {}, your_name: m.your_name, name: meta.name || base };
  setTitle(current.name);
  [...listEl.children].forEach((li) => li.classList && li.classList.toggle("active", li.dataset && li.dataset.base === base));

  // Re-interpret is possible whenever the transcript was saved (the paid step is done).
  const canReinterpret = (m.transcript || []).length > 0;

  let html = "";
  if (m.state === "error") {
    html += `<div class="status-line error">Couldn’t finish this one: ${esc(m.error || "unknown error")}</div>`;
    if (canReinterpret) html += `<div class="error-actions"><button class="link-btn" id="reinterpret-btn">Re-interpret (free)</button></div>`;
  } else if (PROCESSING.has(m.state)) {
    html += decodingPanel();
  }

  if (m.report_md) {
    try { html += renderReport(m.report_md); }
    catch (_) { html += `<div class="report-fallback">${m.report_html || ""}</div>`; }
    html += `<div class="report-hr"></div>
      <div class="report-actions">
        <button class="link-btn" id="rename-btn">Rename</button>
        <button class="link-btn" id="export-btn">Export</button>
        ${canReinterpret ? `<button class="link-btn" id="reinterpret-btn">Re-interpret</button>` : ""}
      </div>`;
  } else if (m.report_html) {
    html += `<div class="report-fallback">${m.report_html}</div>`;
  } else if (m.state === "parked") {
    html += `<div class="parked-cta reveal">
        <p class="parked-q">This recording hasn’t been summarized yet.</p>
        <p class="parked-sub">Summarize it whenever you’re ready.</p>
        <button class="cta" id="decode-btn">Summarize now</button>
      </div>`;
  } else if (!PROCESSING.has(m.state) && m.state !== "error") {
    html += `<p class="report-empty">No report yet for this meeting.</p>`;
  }
  reportEl.innerHTML = html;
  revealIn(reportEl);

  primePlayer();  // ready the audio so Play works without a citation

  const decodeBtn = $("#decode-btn");
  if (decodeBtn) decodeBtn.onclick = () => decodeMeeting(base);
  const reBtn = $("#reinterpret-btn");
  if (reBtn) reBtn.onclick = () => reinterpretMeeting(base);
  const renameBtn = $("#rename-btn");
  if (renameBtn) renameBtn.onclick = startRename;
  const exportBtn = $("#export-btn");
  if (exportBtn) exportBtn.onclick = () => exportReport(base, m.report_md);

  if (PROCESSING.has(m.state)) pollMeeting(base);
  if (!quiet) document.getElementById("workspace").scrollTo({ top: 0, behavior: "smooth" });
}

function decodingPanel() {
  return `<div class="decoding-panel reveal">
      <div class="decoding-badge"><span class="dot"></span>DECODING</div>
      <h2>Decoding your meeting…</h2>
      <p>Separating the two voices and reading between the lines. This usually takes under a minute.</p>
      <div class="shimmer-bars"><i style="width:90%"></i><i style="width:100%"></i><i style="width:74%"></i></div>
    </div>`;
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

/* ── custom two-track player ─────────────────────────────────────────── */
function resolveTrack(mode) {
  const t = (current && current.tracks) || {};
  if (mode === "mic" && t.mic) return "mic";
  if (mode === "system" && t.system) return "system";
  if (mode === "mix" && t.mix) return "mix";
  return t.mix ? "mix" : t.single ? "single" : t.system ? "system" : t.mic ? "mic" : null;
}

function trackSrc(track) {
  const url = `${serverUrl()}/api/audio/${encodeURIComponent(current.base)}/${track}`;
  const tok = authToken();
  return tok ? url + `?token=${encodeURIComponent(tok)}` : url;
}

let pendingMeta = null;
function loadTrack(track, { seekSecs = null, autoplay = false } = {}) {
  if (!current || !track) return;
  if (pendingMeta) { player.removeEventListener("loadedmetadata", pendingMeta); pendingMeta = null; }
  const src = trackSrc(track);
  if (player.dataset.src !== src) { player.dataset.src = src; player.src = src; player.load(); }
  if (seekSecs != null) {
    const applySeek = () => { try { player.currentTime = +seekSecs; } catch (_) {} };
    if (player.readyState >= 1) applySeek();
    else {
      pendingMeta = () => { pendingMeta = null; if (player.dataset.src === src) applySeek(); };
      player.addEventListener("loadedmetadata", pendingMeta, { once: true });
    }
  }
  if (autoplay) { const p = player.play(); if (p && p.catch) p.catch(() => {}); }
}

// Seeded bar heights (deterministic per base), mirroring the design's seedBars.
function seededHeights(seed) {
  const a = [];
  for (let i = 0; i < NB; i++) {
    const x = Math.sin((i + 1) * 12.9898 + seed * 78.233) * 43758.5453;
    a.push(3 + Math.round((x - Math.floor(x)) * 15));
  }
  return a;
}
function hashStr(s) { let h = 0; for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) | 0; return Math.abs(h) % 997 + 1; }

function renderWave(base) {
  const seed = hashStr(base || "x");
  const youH = seededHeights(seed), themH = seededHeights(seed + 7);
  const build = (heights, rowEl) => {
    rowEl.innerHTML = "";
    const bars = [];
    for (const h of heights) {
      const b = document.createElement("i");
      b.style.height = h + "px";
      rowEl.appendChild(b);
      bars.push(b);
    }
    return bars;
  };
  waveBars.you = build(youH, $("#wave-you"));
  waveBars.them = build(themH, $("#wave-them"));
  paintWave();
}

function paintWave() {
  const dur = player.duration || 0;
  const t = player.currentTime || 0;
  const frac = dur ? t / dur : 0;
  const fill = (bars, color) => {
    for (let i = 0; i < bars.length; i++) bars[i].style.background = (i / NB) <= frac ? color : "var(--wave-off)";
  };
  fill(waveBars.you, "var(--accent-played)");
  fill(waveBars.them, "var(--coral-played)");
  if (current && dur && isFinite(dur) && meetingDur[current.base] !== dur) {
    meetingDur[current.base] = dur;   // cache the real length → refresh the sidebar label once
    renderMeetingList();
  }
  const ph = $("#wave-playhead");
  ph.style.left = (Math.min(1, frac) * 100) + "%";
  ph.style.opacity = (player.currentTime > 0 || !player.paused) ? 0.45 : 0;
  $("#t-cur").textContent = fmtTime(t);
  $("#t-tot").textContent = fmtTime(dur);
  paintActive(t);
}

function paintActive(t) {
  const playing = !player.paused;
  document.querySelectorAll(".action[data-ts]").forEach((el) => {
    const ts = parseFloat(el.dataset.ts);
    const on = playing && t >= ts && (t - ts) < 1.6;
    el.classList.toggle("active", on);
  });
}

function applyTrackDim() {
  $("#wave-you").style.opacity = trackMode === "system" ? 0.16 : 1;
  $("#wave-them").style.opacity = trackMode === "mic" ? 0.16 : 1;
}
function trackWord() { return trackMode === "mic" ? "you" : trackMode === "system" ? "them" : "mix"; }

function setFileLabel() {
  if (!current) { playerFile.textContent = "No meeting playing"; return; }
  playerFile.textContent = `${current.base} · ${trackWord()}`;
}

function primePlayer() {
  lastClip = null;
  document.querySelectorAll(".pill.playing").forEach((c) => c.classList.remove("playing"));
  player.pause();
  renderWave(current.base);
  applyTrackDim();
  const track = resolveTrack(trackMode);
  if (!track) {
    if (pendingMeta) { player.removeEventListener("loadedmetadata", pendingMeta); pendingMeta = null; }
    player.removeAttribute("src"); player.dataset.src = ""; player.load();
    playerFile.textContent = `${current.base} · no audio`;
    paintWave();
    return;
  }
  loadTrack(track);
  setFileLabel();
}

function seekTo(ts, chip) {
  if (!current) return;
  const track = resolveTrack(trackMode);
  if (!track) { toast("No audio was recorded for this meeting.", "err"); return; }
  lastClip = { ts, chip };
  document.querySelectorAll(".pill.playing").forEach((c) => c.classList.remove("playing"));
  if (chip) chip.classList.add("playing");
  loadTrack(track, { seekSecs: +ts, autoplay: true });
}

// report pills seek the player
reportEl.addEventListener("click", (ev) => {
  const box = ev.target.closest(".act-box");
  if (box) {
    const checked = box.classList.toggle("checked");
    box.setAttribute("aria-checked", checked ? "true" : "false");
    box.closest(".action").classList.toggle("checked", checked);
    box.innerHTML = checked ? "✓" : "";
    return;
  }
  const chip = ev.target.closest(".pill, .cite");
  if (chip && current) { const ts = parseFloat(chip.dataset.ts); if (!isNaN(ts)) seekTo(ts, chip); }
});

// play / pause
$("#play-btn").onclick = () => {
  if (!current) return;
  if (player.paused) { const p = player.play(); if (p && p.catch) p.catch(() => {}); }
  else player.pause();
};

// click waveform to seek
$("#wave").onclick = (e) => {
  if (!current) return;
  const track = resolveTrack(trackMode);
  if (!track) return;
  const r = e.currentTarget.getBoundingClientRect();
  const f = Math.max(0, Math.min(1, (e.clientX - r.left) / r.width));
  if (player.duration) { player.currentTime = f * player.duration; if (player.paused) { const p = player.play(); if (p && p.catch) p.catch(() => {}); } }
  else loadTrack(track, { autoplay: true });
};

// track segment (Mix / You / Them)
document.querySelectorAll("#track-seg button").forEach((btn) => {
  btn.onclick = () => {
    document.querySelectorAll("#track-seg button").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    trackMode = btn.dataset.mode;
    applyTrackDim();
    if (!current) return;
    const track = resolveTrack(trackMode);
    setFileLabel();
    if (!track) return;
    const at = player.currentTime || 0;
    const wasPlaying = !player.paused;
    loadTrack(track, { seekSecs: at, autoplay: wasPlaying });
  };
});

// volume + mute
const vol = $("#vol");
function reflectMute() {
  const muted = player.muted || player.volume === 0;
  // Use explicit display, not the `hidden` property — SVGElement doesn't honor it.
  $("#vol-btn .ic-on").style.display = muted ? "none" : "block";
  $("#vol-btn .ic-off").style.display = muted ? "block" : "none";
}
vol.addEventListener("input", () => { player.volume = parseFloat(vol.value); player.muted = player.volume === 0; reflectMute(); });
$("#vol-btn").onclick = () => {
  player.muted = !player.muted;
  if (!player.muted && player.volume === 0) { player.volume = 0.8; vol.value = "0.8"; }
  reflectMute();
};
player.volume = 0.8;

player.addEventListener("timeupdate", paintWave);
player.addEventListener("durationchange", paintWave);
player.addEventListener("loadedmetadata", paintWave);
player.addEventListener("play", () => playerBar.classList.add("playing"));
player.addEventListener("pause", () => playerBar.classList.remove("playing"));
player.addEventListener("ended", () => {
  playerBar.classList.remove("playing");
  document.querySelectorAll(".pill.playing").forEach((c) => c.classList.remove("playing"));
  paintWave();
});

/* ── record control (native capture via Tauri → upload) ──────────────── */
// Web only: open the connected Mac app via the deep link. If the tab doesn't background
// within ~1.5s the app didn't actually launch (e.g. uninstalled since it was last seen),
// so fall back to the "get the Mac app" modal — no dead end.
function openDesktopApp() {
  let opened = false;
  const onHide = () => { if (document.visibilityState === "hidden") opened = true; };
  document.addEventListener("visibilitychange", onHide);
  toast("Opening Gotcha…", "ok");
  window.location.href = "gotcha://open";
  setTimeout(() => {
    document.removeEventListener("visibilitychange", onHide);
    if (!opened) openRecordModal();
  }, 1500);
}

async function startRecording() {
  if (!TAURI) { hasDesktop() ? openDesktopApp() : openRecordModal(); return; }
  if (!authToken()) { openSettings(); return; }
  recBtn.disabled = true;
  try {
    recSession = await invoke("start_recording", { name: DEFAULT_REC_NAME });
  } catch (e) {
    const msg = String(e);
    if (/[Pp]ermission/.test(msg)) openPerm();
    else toast("Couldn't start recording: " + msg, "err");
    recBtn.disabled = false; return;
  }
  recording = true;
  recText.textContent = "Stop";
  recBtn.classList.add("recording");
  recBtn.disabled = false;
  recTimer.hidden = false;
  let secs = 0; recTimer.textContent = "00:00";
  timerId = setInterval(() => { secs++; recTimer.textContent = String((secs / 60 | 0)).padStart(2, "0") + ":" + String(secs % 60).padStart(2, "0"); }, 1000);
}

async function stopRecording() {
  recBtn.disabled = true;
  if (timerId) { clearInterval(timerId); timerId = null; }
  let sess;
  try { sess = await invoke("stop_recording"); }
  catch (e) { toast("Stop failed: " + e, "err"); resetRecUI(); return; }
  resetRecUI();
  recSession = null;
  pendingRec = sess;
  openPostRec();
}

async function finishUpload(processNow) {
  const sess = pendingRec; pendingRec = null;
  if (!sess) return;
  closePostRec();
  const glossary = ($("#post-glossary").value || "").trim();
  recBtn.disabled = true;
  let serverBase;
  try {
    serverBase = await invoke("upload_recording", {
      serverUrl: serverUrl(), token: authToken(),
      name: DEFAULT_REC_NAME,
      systemPath: sess.system_path, micPath: sess.mic_path,
      glossary, process: processNow,
    });
  } catch (e) { toast("Upload failed: " + e, "err"); recBtn.disabled = false; return; }
  recBtn.disabled = false;
  $("#post-glossary").value = "";
  await loadMeetings();
  if (serverBase) openMeeting(serverBase);
}

function openPostRec() { const d = $("#postrec"); d.showModal ? d.showModal() : (d.hidden = false); }
function closePostRec() { const d = $("#postrec"); d.close ? d.close() : (d.hidden = true); }

async function decodeMeeting(base) {
  try { await api("/api/process/" + encodeURIComponent(base), { method: "POST" }); }
  catch (e) { toast("Couldn't start decoding: " + e.message, "err"); return; }
  await loadMeetings();
  openMeeting(base);
}

async function reinterpretMeeting(base) {
  try { await api("/api/reinterpret/" + encodeURIComponent(base), { method: "POST" }); }
  catch (e) { toast("Couldn't start re-interpret: " + e.message, "err"); return; }
  await loadMeetings();
  openMeeting(base);
}

async function deleteMeeting(base) {
  if (!confirm(`Delete this meeting permanently?\n\n${base}\n\n` +
      `This removes its recording, transcript and report. This can’t be undone.`)) return;
  try { await api("/api/meetings/" + encodeURIComponent(base), { method: "DELETE" }); }
  catch (e) { toast("Couldn't delete: " + e.message, "err"); return; }
  if (current && current.base === base) { current = null; reportEl.innerHTML = ""; setTitle(null); primePlayerEmpty(); }
  await loadMeetings();
}
function primePlayerEmpty() {
  player.pause(); player.removeAttribute("src"); player.dataset.src = "";
  $("#wave-you").innerHTML = ""; $("#wave-them").innerHTML = "";
  setFileLabel();
}

function resetRecUI() {
  recording = false;
  if (timerId) { clearInterval(timerId); timerId = null; }
  recText.textContent = "Record";
  recBtn.classList.remove("recording");
  recBtn.disabled = false;
  recTimer.hidden = true;
}
recBtn.onclick = () => (recording ? stopRecording() : startRecording());
$("#post-jump").onclick = () => finishUpload(true);
$("#post-park").onclick = () => finishUpload(false);

/* ── record modal (web) ──────────────────────────────────────────────── */
function openRecordModal() { $("#record-modal").hidden = false; }
function closeRecordModal() { $("#record-modal").hidden = true; }
$("#record-modal-close").onclick = closeRecordModal;
$("#record-modal").addEventListener("click", (e) => { if (e.target.id === "record-modal") closeRecordModal(); });

/* ── settings: web popover vs desktop connect dialog ─────────────────── */
const PREF_KEYS = ["twoTrack", "autoDecode", "sound"];
function loadPrefs() {
  document.querySelectorAll("#settings-pop .toggle-row").forEach((row) => {
    const k = row.dataset.pref;
    const on = localStorage.getItem("gotcha_pref_" + k) === "1";
    row.querySelector(".knob").classList.toggle("on", on);
  });
}
document.querySelectorAll("#settings-pop .toggle-row").forEach((row) => {
  row.onclick = () => {
    const k = row.dataset.pref;
    const knob = row.querySelector(".knob");
    const on = !knob.classList.contains("on");
    knob.classList.toggle("on", on);
    localStorage.setItem("gotcha_pref_" + k, on ? "1" : "0");
  };
});

let currentUser = null;
// Paint whichever account UI is present — web popover (#acct-*) and/or the
// desktop account dialog (#d-acct-*).
function paintAccount() {
  const u = currentUser || {};
  const email = u.email || u.display_name || "Signed in";
  const avatar = (email[0] || "G").toUpperCase();
  const used = u.used_min != null ? Math.round(u.used_min) : 0;
  const cap = u.cap_min != null ? Math.round(u.cap_min) : 0;
  const usage = cap ? `${used} of ${cap} min used` : `${used} min used`;
  [["#acct-email", "#acct-avatar", "#acct-usage"],
   ["#d-acct-email", "#d-acct-avatar", "#d-acct-usage"]].forEach(([eSel, aSel, uSel]) => {
    const e = $(eSel), a = $(aSel), us = $(uSel);
    if (e) e.textContent = email;
    if (a) a.textContent = avatar;
    if (us) us.textContent = usage;
  });
  // Web: only offer "Connect the desktop app" until one is connected; then show the
  // connected state instead. (has_desktop comes from /api/auth/me.)
  const connected = !!u.has_desktop;
  const ca = $("#connect-app"); if (ca) ca.hidden = connected;
  const ds = $("#desktop-status"); if (ds) ds.hidden = !connected;
}
const hasDesktop = () => !!(currentUser && currentUser.has_desktop);
async function fillAccount() {
  try { currentUser = await api("/api/auth/me"); } catch (_) {}
  paintAccount();
}
function toggleSettingsPop() {
  const pop = $("#settings-pop");
  if (pop.hidden) { loadPrefs(); fillAccount(); pop.hidden = false; }
  else pop.hidden = true;
}
function showWelcome() { const w = $("#welcome"); if (w) w.hidden = false; }
function hideWelcome() { const w = $("#welcome"); if (w) w.hidden = true; }

async function signOut() {
  if (TAURI) {
    // Desktop has no cookie — sign out is just dropping the local bearer token,
    // then back to the welcome screen.
    localStorage.removeItem("gotcha_token");
    localStorage.removeItem("gotcha_server");
    currentUser = null;
    closeSettings();
    showWelcome();
    return;
  }
  try { await api("/api/auth/logout", { method: "POST" }); } catch (_) {}
  location.replace("/login");
}
const webSignout = $("#acct-signout"); if (webSignout) webSignout.onclick = signOut;
const dSignout = $("#d-acct-signout"); if (dSignout) dSignout.onclick = signOut;

// Web only: hand the just-installed desktop app a session. The connect endpoint
// reuses this browser's web session (no second login) and fires the gotcha:// deep link.
const connectApp = $("#connect-app");
if (connectApp) connectApp.onclick = () => window.open("/api/auth/desktop/connect", "_blank");

// Desktop keeps the server/token connect dialog; web gets the popover.
$("#settings-btn").onclick = () => (TAURI ? openSettings() : toggleSettingsPop());

// close popover on outside-click / Esc
document.addEventListener("click", (e) => {
  const pop = $("#settings-pop");
  if (!pop || pop.hidden) return;
  if (!pop.contains(e.target) && e.target.id !== "settings-btn" && !$("#settings-btn").contains(e.target)) pop.hidden = true;
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") { const pop = $("#settings-pop"); if (pop && !pop.hidden) pop.hidden = true; if (!$("#record-modal").hidden) closeRecordModal(); }
});

/* ── desktop account dialog (signed-in: email, usage, sign out) ──────── */
function openSettings() {
  const dlg = $("#settings");
  fillAccount();
  dlg.showModal ? dlg.showModal() : (dlg.hidden = false);
}
function closeSettings() {
  const dlg = $("#settings");
  dlg.close ? dlg.close() : (dlg.hidden = true);
}
const setClose = $("#settings-close");
if (setClose) setClose.onclick = closeSettings;

// Open the hosted sign-in page in the system browser; the gotcha:// deep link
// brings the token back (handled by applyConnectUrl).
async function startBrowserSignin() {
  localStorage.setItem("gotcha_server", DEFAULT_SERVER);
  if (!invoke) { toast("Sign-in needs the Gotcha desktop app.", "err"); return; }
  try { await invoke("open_signin", { serverUrl: DEFAULT_SERVER }); }
  catch (e) { toast("Couldn't open the browser: " + e, "err"); return; }
  const status = $("#welcome-status");
  if (status) status.textContent = "Finish signing in in your browser…";
  toast("Finish signing in in your browser.", "ok");
}
const welcomeSignin = $("#welcome-signin");
if (welcomeSignin) welcomeSignin.onclick = startBrowserSignin;

/* ── zero-paste onboarding: gotcha://connect?server=&token= ──────────── */
function applyConnectUrl(raw) {
  try {
    const u = new URL(raw);
    const server = u.searchParams.get("server");
    const token = u.searchParams.get("token");
    if (server) localStorage.setItem("gotcha_server", server.replace(/\/+$/, ""));
    if (token) localStorage.setItem("gotcha_token", token);
    closeSettings();
    hideWelcome();
    fillAccount();
    loadMeetings(true);
    return true;
  } catch (_) { return false; }
}
if (TAURI && window.__TAURI__.event) {
  window.__TAURI__.event.listen("deep-link", (e) => applyConnectUrl(e.payload));
}

/* ── permission wizard (opened when capture is blocked) ──────────────── */
function openPerm() { const dlg = $("#perm"); dlg.showModal ? dlg.showModal() : (dlg.hidden = false); }
document.querySelectorAll("#perm [data-pane]").forEach((b) => {
  b.onclick = () => { if (invoke) invoke("open_privacy_pane", { which: b.dataset.pane }); };
});
const permClose = $("#perm-close");
if (permClose) permClose.onclick = () => { const d = $("#perm"); d.close ? d.close() : (d.hidden = true); };
const permRelaunch = $("#perm-relaunch");
if (permRelaunch) permRelaunch.onclick = () => { if (invoke) invoke("relaunch"); };

/* ── sidebar collapse / expand ───────────────────────────────────────── */
function setSidebar(open) {
  $("#sidebar").hidden = !open;
  $("#rail").hidden = open;
}
$("#collapse-btn").onclick = () => setSidebar(false);
$("#reopen-btn").onclick = () => setSidebar(true);

/* ── motion: one orchestrated reveal ─────────────────────────────────── */
function revealIn(scope) {
  const els = (scope || document).querySelectorAll(".reveal:not(.in)");
  els.forEach((el, i) => setTimeout(() => el.classList.add("in"), 60 + i * 70));
}

// Filter the meetings rail as you type.
const searchEl = $("#meeting-search");
if (searchEl) searchEl.addEventListener("input", renderMeetingList);

/* ── desktop: keep the webview on the app shell ──────────────────────── */
// The static pages (landing, login, download) all live in the same bundle and
// are cross-linked with relative hrefs. Inside Tauri we must never navigate to
// them — internal links are neutralised, external links open in the browser.
if (TAURI) {
  const brand = document.querySelector(".brand");
  if (brand) brand.setAttribute("href", "#");
  document.addEventListener("click", (e) => {
    const a = e.target.closest && e.target.closest("a[href]");
    if (!a) return;
    const href = a.getAttribute("href") || "";
    if (href === "" || href.startsWith("#")) return;     // in-page anchors are fine
    e.preventDefault();                                  // never leave app.html
    if (/^https?:\/\//i.test(href) && invoke) {          // external → system browser
      invoke("open_external", { url: href }).catch(() => {});
    }
  }, true);
}

/* ── boot ────────────────────────────────────────────────────────────── */
reflectMute();
revealIn(document);
// Desktop first run (no token yet): show the welcome screen synchronously so the
// empty app shell never flashes before /api/auth/me resolves.
if (TAURI && !authToken()) showWelcome();
async function boot() {
  try {
    currentUser = await api("/api/auth/me");
    if (TAURI) { hideWelcome(); paintAccount(); }
  } catch (e) {
    if (!TAURI) { location.replace("/login"); return; }
    // Desktop: /me failed → no valid session. Drop any stale token so we never
    // show a previous user's residue, then show the welcome screen.
    if (authToken()) {
      localStorage.removeItem("gotcha_token");
      localStorage.removeItem("gotcha_server");
    }
    currentUser = null;
    showWelcome();
  }
  loadMeetings(true);
  setInterval(() => loadMeetings(false), 8000);
}
boot();
