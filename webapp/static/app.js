"use strict";

/* ── config: backend URL + auth token + native bridge ────────────────── */
const TAURI = !!window.__TAURI__;
const invoke = TAURI ? window.__TAURI__.core.invoke : null;
const DEFAULT_SERVER = "http://localhost:8000";
// Web: the app is served BY the backend, so it's same-origin — use relative paths
// and authenticate by the session cookie. Desktop: a different origin (tauri://),
// so it targets the stored server URL and authenticates by bearer token.
const serverUrl = () =>
  TAURI ? (localStorage.getItem("gotcha_server") || DEFAULT_SERVER).replace(/\/+$/, "") : "";
const authToken = () => (TAURI ? localStorage.getItem("gotcha_token") || "" : "");

/* ── state ───────────────────────────────────────────────────────────── */
let current = null;       // { base, transcript, tracks, your_name }
let recSession = null;    // { base, system_path, mic_path } from native start
let pendingRec = null;    // captured-but-not-yet-uploaded session (awaiting the post-rec modal)
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

/* ── toasts (in-app feedback, replaces alert) ────────────────────────── */
function toast(msg, kind) {
  const wrap = document.getElementById("toasts");
  if (!wrap) { console.warn(msg); return; }
  const el = document.createElement("div");
  el.className = "toast" + (kind ? " " + kind : "");
  el.textContent = msg;
  wrap.appendChild(el);
  requestAnimationFrame(() => el.classList.add("in"));
  setTimeout(() => { el.classList.remove("in"); setTimeout(() => el.remove(), 220); }, 3600);
}

/* ── tiny helpers ────────────────────────────────────────────────────── */
async function api(path, opts) {
  opts = opts || {};
  const headers = Object.assign({}, opts.headers);
  const tok = authToken();
  if (tok) headers["Authorization"] = "Bearer " + tok;
  // Web is same-origin, so the session cookie is sent by default (no
  // credentials:'include' — that would trip the wildcard-CORS rule on the
  // cross-origin desktop webview, which authenticates by bearer header anyway).
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
  s = Math.max(0, Math.floor(s));
  return String((s / 60) | 0).padStart(2, "0") + ":" + String(s % 60).padStart(2, "0");
};
const pillHTML = (ts) =>
  `<span class="pill" data-ts="${ts}"><span class="pill-rec"></span>${ts}s<span class="pill-play">▶</span></span>`;
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

    // How is often emitted as a header with nested sub-bullets (one param per
    // line), so collect every line after **How:** until the next known key.
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
    // one param per line; also fold "key: value; key: value" inline lists
    const howHTML = howLines.map((l) => mdInline(l).replace(/;\s+/g, "<br>")).join("<br>");
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
          ${howHTML ? `<details class="act-how" open><summary>how to do it</summary><div class="act-how-body">${howHTML}</div></details>` : ""}
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
    let quote = "";
    for (const s of it.subs) {
      const m = s.match(/^\*\*\s*Source:?\s*\*\*\s*(.*)/i);
      if (m) { quote = m[1].replace(/\[[^\]]*\]/, "").trim().replace(/^[“"']|[”"']$/g, ""); break; }
    }
    return `<div class="q-card">
        <p class="q-text">${mdInline(q)}</p>
        ${ts ? `<div class="act-source">${pillHTML(ts)}${quote ? `<span class="act-quote">“${esc(quote)}”</span>` : ""}</div>` : ""}
      </div>`;
  }).join("");
}

const EYEBROW = { summary: "the gist", decode: "reading between the lines", actions: "your move", questions: "before you act" };
function classify(title, idx) {
  const t = title.toLowerCase();
  if (/really meant|lead/.test(t)) return "decode";
  if (/open question|to verify|unresolved|to confirm/.test(t)) return "questions";
  if (/action item|your task|to.?do/.test(t)) return "actions";
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
    const inner = kind === "decode" ? renderDecode(body)
      : kind === "actions" ? renderActions(body)
      : kind === "questions" ? renderQuestions(body)
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
  if (state === "parked") return '<span class="badge parked">parked</span>';
  if (PROCESSING.has(state)) return `<span class="badge processing">${state}</span>`;
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
    li.textContent = q ? "No meetings match." : "No meetings yet — hit Record.";
    listEl.appendChild(li);
    return;
  }
  for (const m of shown) {
    const li = document.createElement("li");
    if (current && current.base === m.base) li.classList.add("active");
    li.dataset.base = m.base;
    li.innerHTML =
      `<div class="mi-name">${esc(m.name || m.base)}</div>` +
      `<div class="mi-meta"><span class="mi-date">${esc(fmtDate(m.created))}</span>` +
      `${badge(m.state, m.has_report)}</div>` +
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

/* rename (display name only — base/files unchanged) + export the report markdown */
async function renameMeeting(base) {
  const m = allMeetings.find((x) => x.base === base);
  let name = null;
  try { name = window.prompt("Rename meeting", (m && m.name) || base); } catch (_) {}
  if (name == null) return;
  name = name.trim();
  if (!name) return;
  try {
    await api("/api/meetings/" + encodeURIComponent(base), {
      method: "PATCH", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
  } catch (e) { toast("Couldn't rename: " + e.message, "err"); return; }
  toast("Renamed", "ok");
  await loadMeetings();
}

function exportReport(base, mdText) {
  if (!mdText) { toast("No report to export yet.", "err"); return; }
  const m = allMeetings.find((x) => x.base === base);
  const fname = ((m && m.name) || base).replace(/[^\w-]+/g, "_") + ".md";
  const blob = new Blob([mdText], { type: "text/markdown" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob); a.download = fname;
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  toast("Exported " + fname, "ok");
}

async function openMeeting(base, quiet) {
  let m;
  try { m = await api("/api/meetings/" + encodeURIComponent(base)); }
  catch (e) { reportEl.innerHTML = `<div class="status-line error">${esc(e.message)}</div>`; return; }

  current = { base, transcript: m.transcript || [], tracks: m.tracks || {}, your_name: m.your_name };
  [...listEl.children].forEach((li) => li.classList.toggle("active", li.dataset.base === base));

  // Re-interpret is possible whenever the transcript was saved (the paid step is
  // done) — recovers a failed/old report for free, no re-record, no Sarvam.
  const canReinterpret = (m.transcript || []).length > 0;

  let html = "";
  if (m.state === "error") {
    html += `<div class="status-line error">Couldn’t finish this one: ${esc(m.error || "unknown error")}</div>`;
    if (canReinterpret) html += `<div class="error-actions"><button class="link-btn" id="reinterpret-btn">Re-interpret (free)</button></div>`;
  } else if (PROCESSING.has(m.state)) {
    html += `<div class="status-line">Working on it — ${esc(m.state)}. This takes a few minutes.</div>`;
  }

  if (m.report_md) {
    try { html += renderReport(m.report_md); }
    catch (_) { html += `<div class="report-fallback">${m.report_html || ""}</div>`; }
    html += `<div class="report-actions">
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

  primePlayer();  // ready the audio so Play works without needing a timestamp

  const decodeBtn = $("#decode-btn");
  if (decodeBtn) decodeBtn.onclick = () => decodeMeeting(base);

  const reBtn = $("#reinterpret-btn");
  if (reBtn) reBtn.onclick = () => reinterpretMeeting(base);

  const renameBtn = $("#rename-btn");
  if (renameBtn) renameBtn.onclick = () => renameMeeting(base);
  const exportBtn = $("#export-btn");
  if (exportBtn) exportBtn.onclick = () => exportReport(base, m.report_md);

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

function trackSrc(track) {
  const url = `${serverUrl()}/api/audio/${encodeURIComponent(current.base)}/${track}`;
  // Desktop sends the token in the query (the <audio> element can't set a header);
  // web is same-origin so the session cookie rides along automatically.
  const tok = authToken();
  return tok ? url + `?token=${encodeURIComponent(tok)}` : url;
}

let pendingMeta = null;  // a not-yet-fired loadedmetadata seek handler from a prior load

// Point the player at a track. seekSecs: jump there once seekable; autoplay: play after.
// Seek and play are decoupled so a stale handler from a previous meeting/track can never
// fire against the new source, and an interrupted play() never leaves a phantom state.
function loadTrack(track, { seekSecs = null, autoplay = false } = {}) {
  if (!current || !track) return;
  // Drop any seek handler still pending from an earlier load — it belongs to old audio.
  if (pendingMeta) { player.removeEventListener("loadedmetadata", pendingMeta); pendingMeta = null; }

  const src = trackSrc(track);
  if (player.dataset.src !== src) {
    player.dataset.src = src; player.src = src; player.load();
  }

  if (seekSecs != null) {
    const applySeek = () => { try { player.currentTime = +seekSecs; } catch (_) {} };
    if (player.readyState >= 1) applySeek();          // metadata already available
    else {
      pendingMeta = () => {
        pendingMeta = null;
        if (player.dataset.src === src) applySeek();  // guard: only the intended source
      };
      player.addEventListener("loadedmetadata", pendingMeta, { once: true });
    }
  }

  if (autoplay) {
    const p = player.play();                          // kicks the fetch under preload="none"
    if (p && p.catch) p.catch(() => {});              // swallow AbortError on rapid switches
  }
}

// Prime the player when a meeting opens so the native play button works even
// without clicking a citation (and for reports that have no citations at all).
function primePlayer() {
  lastClip = null;
  document.querySelectorAll(".pill.playing").forEach((c) => c.classList.remove("playing"));
  player.pause();
  const track = resolveTrack(trackMode);
  if (!track) {
    if (pendingMeta) { player.removeEventListener("loadedmetadata", pendingMeta); pendingMeta = null; }
    player.removeAttribute("src"); player.dataset.src = ""; player.load();
    playerLabel.textContent = "No audio for this meeting.";
    return;
  }
  loadTrack(track);  // set source, ready to play from the start
  playerLabel.textContent = `${current.base} · ${track}`;
}

function seekTo(ts, chip) {
  if (!current) return;
  const track = resolveTrack(trackMode);
  if (!track) { playerLabel.textContent = "No audio was recorded for this meeting."; return; }
  lastClip = { ts, chip };
  document.querySelectorAll(".pill.playing").forEach((c) => c.classList.remove("playing"));
  if (chip) chip.classList.add("playing");
  playerLabel.textContent = `${current.base} · ${track} · ${(+ts).toFixed(1)}s`;
  loadTrack(track, { seekSecs: +ts, autoplay: true });
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
    if (!current) return;
    const track = resolveTrack(trackMode);
    if (!track) return;
    // Re-point to the chosen side, preserving position + play state — no citation needed.
    const at = player.currentTime || 0;
    const wasPlaying = !player.paused;
    loadTrack(track, { seekSecs: at, autoplay: wasPlaying });
    playerLabel.textContent = lastClip
      ? `${current.base} · ${track} · ${(+lastClip.ts).toFixed(1)}s`
      : `${current.base} · ${track}`;
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
  if (!TAURI) { toast("Recording needs the Gotcha desktop app.", "err"); return; }
  if (!authToken()) { openSettings(); return; }
  recBtn.disabled = true;
  try {
    recSession = await invoke("start_recording", { name: recName.value || "meeting" });
  } catch (e) {
    const msg = String(e);
    if (/[Pp]ermission/.test(msg)) openPerm();   // guide the grant instead of a dead-end toast
    else toast("Couldn't start recording: " + msg, "err");
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
  catch (e) { toast("Stop failed: " + e, "err"); resetRecUI(); return; }
  resetRecUI();
  recSession = null;
  pendingRec = sess;     // capture is on disk; let the user add a glossary + decide
  openPostRec();
}

// Upload the just-captured tracks. processNow=true → decode immediately;
// false → park it (stored, decode later from the catch-up CTA).
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
      name: recName.value || "meeting",
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

// Start decoding a parked meeting (audio already uploaded).
async function decodeMeeting(base) {
  try { await api("/api/process/" + encodeURIComponent(base), { method: "POST" }); }
  catch (e) { toast("Couldn't start decoding: " + e.message, "err"); return; }
  await loadMeetings();
  openMeeting(base);
}

// Re-run ONLY the interpret step on a saved transcript (free — no Sarvam). Used to
// recover a meeting whose interpret failed, or to refresh an existing report.
async function reinterpretMeeting(base) {
  try { await api("/api/reinterpret/" + encodeURIComponent(base), { method: "POST" }); }
  catch (e) { toast("Couldn't start re-interpret: " + e.message, "err"); return; }
  await loadMeetings();
  openMeeting(base);  // state is now "interpreting" → openMeeting starts polling
}

// Permanently delete a meeting (audio + report + transcript). Irreversible, so it
// asks first. Usage already billed isn't refunded (Sarvam was already paid).
async function deleteMeeting(base) {
  if (!confirm(`Delete this meeting permanently?\n\n${base}\n\n` +
      `This removes its recording, transcript and report. ` +
      `This can’t be undone.`)) return;
  try { await api("/api/meetings/" + encodeURIComponent(base), { method: "DELETE" }); }
  catch (e) { toast("Couldn't delete: " + e.message, "err"); return; }
  if (current && current.base === base) { current = null; reportEl.innerHTML = ""; }
  await loadMeetings();
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
$("#post-jump").onclick = () => finishUpload(true);
$("#post-park").onclick = () => finishUpload(false);

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
$("#settings-form").addEventListener("submit", saveSettings);

// Desktop: open the hosted login in the system browser. The server's post-login
// redirect (gotcha://connect?server=&token=) binds us via the deep-link handler.
const signinBtn = $("#signin-btn");
if (signinBtn) signinBtn.onclick = async () => {
  const server = ($("#set-server").value.trim() || DEFAULT_SERVER).replace(/\/+$/, "");
  localStorage.setItem("gotcha_server", server);   // so the deep-link return matches
  if (!invoke) { toast("Sign-in needs the Gotcha desktop app.", "err"); return; }
  try { await invoke("open_signin", { serverUrl: server }); }
  catch (e) { toast("Couldn't open the browser: " + e, "err"); return; }
  toast("Finish signing in in your browser.", "ok");
};

/* ── account menu (web: cookie session) ──────────────────────────────── */
let currentUser = null;
async function openAccount() {
  try { currentUser = await api("/api/auth/me"); } catch (_) {}
  const u = currentUser || {};
  const emailEl = $("#acct-email");
  if (emailEl) emailEl.textContent = u.email || u.display_name || "Signed in";
  const usageEl = $("#acct-usage");
  if (usageEl) {
    const used = u.used_min != null ? u.used_min : 0;
    const cap = u.cap_min != null ? u.cap_min : 0;
    usageEl.textContent = cap ? `${used} of ${cap} min used` : `${used} min used`;
  }
  const dlg = $("#account");
  if (dlg) dlg.showModal ? dlg.showModal() : (dlg.hidden = false);
}
async function signOut() {
  try { await api("/api/auth/logout", { method: "POST" }); } catch (_) {}
  location.replace("/login");
}
// Desktop keeps the server/token connect panel; web gets the account menu.
$("#settings-btn").onclick = () => (TAURI ? openSettings() : openAccount());
const acctClose = $("#acct-close");
if (acctClose) acctClose.onclick = () => { const d = $("#account"); d.close ? d.close() : (d.hidden = true); };
const acctSignout = $("#acct-signout");
if (acctSignout) acctSignout.onclick = signOut;

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

// Legacy hero CTA only exists on the old combined page; guard for app.html.
const heroCta = $("#hero-cta");
if (heroCta) heroCta.onclick = () => {
  document.getElementById("workspace").scrollIntoView({ behavior: "smooth", block: "start" });
  if (!current) loadMeetings(true);
};

// Filter the meetings rail as you type.
const searchEl = $("#meeting-search");
if (searchEl) searchEl.addEventListener("input", renderMeetingList);

/* ── boot ────────────────────────────────────────────────────────────── */
revealIn(document);
async function boot() {
  // Establish identity first. Web: the session cookie decides app-vs-login.
  // Desktop: the bearer token (set via the gotcha:// deep link or settings).
  try {
    currentUser = await api("/api/auth/me");
  } catch (e) {
    if (!TAURI) { location.replace("/login"); return; }  // web: sign in to continue
    if (!authToken()) { openSettings(); }                // desktop: not bound yet
  }
  loadMeetings(true);   // populate sidebar + auto-open newest into the workspace
  setInterval(() => loadMeetings(false), 8000);
}
boot();
