"use strict";

// ---- state ----------------------------------------------------------------
let current = null;       // { base, transcript, tracks, your_name }
let recording = false;
let timerId = null;
let pollId = null;        // active job poll
const PROCESSING = new Set(["queued", "recording", "transcribing", "interpreting"]);

const $ = (sel) => document.querySelector(sel);
const listEl = $("#meeting-list");
const reportEl = $("#report");
const recBtn = $("#rec-btn");
const recTimer = $("#rec-timer");
const recName = $("#rec-name");
const player = $("#player");
const playerLabel = $("#player-label");

// ---- helpers --------------------------------------------------------------
async function api(path, opts) {
  const res = await fetch(path, opts);
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = (await res.json()).detail || msg; } catch (_) {}
    throw new Error(msg);
  }
  return res.status === 204 ? null : res.json();
}

function fmtTime(s) {
  s = Math.max(0, Math.floor(s));
  const m = Math.floor(s / 60);
  return String(m).padStart(2, "0") + ":" + String(s % 60).padStart(2, "0");
}

function stateBadge(state, hasReport) {
  if (state === "error") return '<span class="badge error">error</span>';
  if (PROCESSING.has(state)) return `<span class="badge processing">${state}…</span>`;
  if (hasReport) return '<span class="badge done">ready</span>';
  return "";
}

// ---- meetings list --------------------------------------------------------
async function loadMeetings() {
  let data;
  try { data = await api("/api/meetings"); }
  catch (e) { return; }
  listEl.innerHTML = "";
  for (const m of data.meetings) {
    const li = document.createElement("li");
    if (current && current.base === m.base) li.classList.add("active");
    li.dataset.base = m.base;
    li.innerHTML =
      `<div class="mi-name">${m.base}</div>` +
      `<div class="mi-meta">${stateBadge(m.state, m.has_report)}` +
      `${Object.keys(m.tracks).length ? '<span class="badge">🔊 audio</span>' : ""}</div>`;
    li.onclick = () => openMeeting(m.base);
    listEl.appendChild(li);
  }
}

// ---- open / render a meeting ---------------------------------------------
async function openMeeting(base) {
  let m;
  try { m = await api("/api/meetings/" + encodeURIComponent(base)); }
  catch (e) { reportEl.innerHTML = `<div class="status-line error">${e.message}</div>`; return; }

  current = { base, transcript: m.transcript || [], tracks: m.tracks || {}, your_name: m.your_name };
  [...listEl.children].forEach((li) =>
    li.classList.toggle("active", li.dataset.base === base));

  let html = "";
  if (m.state === "error") {
    html += `<div class="status-line error">Processing failed: ${m.error || "unknown error"}</div>`;
  } else if (PROCESSING.has(m.state)) {
    html += `<div class="status-line">Processing (${m.state}…) — this can take a few minutes.</div>`;
  }
  if (m.report_html) {
    html += m.report_html;
  } else if (!PROCESSING.has(m.state) && m.state !== "error") {
    html += `<div class="empty">No report yet for this meeting.</div>`;
  }
  reportEl.innerHTML = html;

  // Keep refreshing while it's still processing.
  if (PROCESSING.has(m.state)) pollMeeting(base);
}

function pollMeeting(base) {
  if (pollId) clearInterval(pollId);
  pollId = setInterval(async () => {
    let j;
    try { j = await api("/api/jobs/" + encodeURIComponent(base)); }
    catch (_) { return; }
    if (!PROCESSING.has(j.state)) {
      clearInterval(pollId); pollId = null;
      await loadMeetings();
      if (current && current.base === base) openMeeting(base);
    }
  }, 3000);
}

// ---- citation → audio -----------------------------------------------------
let trackMode = "mix";    // "mix" | "mic" (You) | "system" (Them); chosen in the player bar
let lastClip = null;      // { ts, chip } — so the selector can reload the same moment

// Resolve the requested mode to a track that actually exists for this meeting.
function resolveTrack(mode) {
  const t = current.tracks || {};
  if (mode === "mic" && t.mic) return "mic";
  if (mode === "system" && t.system) return "system";
  if (mode === "mix" && t.mix) return "mix";
  // Fallbacks (e.g. single-track meetings, or the requested side wasn't captured).
  return t.mix ? "mix" : (t.single ? "single" : (t.system ? "system" : (t.mic ? "mic" : null)));
}

function seekTo(ts, chip) {
  if (!current) return;
  lastClip = { ts, chip };
  const track = resolveTrack(trackMode);
  if (!track) { playerLabel.textContent = "No audio recorded for this meeting."; return; }

  document.querySelectorAll(".cite.playing").forEach((c) => c.classList.remove("playing"));
  if (chip) chip.classList.add("playing");
  playerLabel.textContent = `${current.base} · ${track} · ${ts.toFixed(1)}s`;

  const src = `/api/audio/${encodeURIComponent(current.base)}/${track}`;
  const go = () => { try { player.currentTime = ts; } catch (_) {} player.play(); };

  if (player.dataset.src !== src) {
    player.dataset.src = src;
    player.src = src;
    player.addEventListener("loadedmetadata", go, { once: true });
    player.load();
  } else {
    go();
  }
}

reportEl.addEventListener("click", (ev) => {
  const chip = ev.target.closest(".cite");
  if (!chip || !current) return;
  const ts = parseFloat(chip.dataset.ts);
  if (!isNaN(ts)) seekTo(ts, chip);
});

// Player-bar track selector: Mix (both) / You (mic) / Them (system).
document.querySelectorAll("#track-seg button").forEach((btn) => {
  btn.onclick = () => {
    document.querySelectorAll("#track-seg button").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    trackMode = btn.dataset.mode;
    if (lastClip) seekTo(lastClip.ts, lastClip.chip);  // replay the same moment on the new side
  };
});

// ---- record control -------------------------------------------------------
async function startRecording() {
  recBtn.disabled = true;
  try {
    await api("/api/record/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: recName.value || "meeting" }),
    });
  } catch (e) {
    alert("Could not start recording:\n" + e.message);
    recBtn.disabled = false;
    return;
  }
  recording = true;
  recBtn.textContent = "■ Stop";
  recBtn.classList.add("recording");
  recBtn.disabled = false;
  recName.disabled = true;
  recTimer.classList.remove("hidden");
  let secs = 0;
  recTimer.textContent = "00:00";
  timerId = setInterval(() => { secs++; recTimer.textContent = fmtTime(secs); }, 1000);
  await loadMeetings();
}

async function stopRecording() {
  recBtn.disabled = true;
  let res;
  try {
    res = await api("/api/record/stop", { method: "POST" });
  } catch (e) {
    alert("Stop failed:\n" + e.message);
    resetRecUI();
    return;
  }
  resetRecUI();
  await loadMeetings();
  if (res && res.base) openMeeting(res.base);  // shows "processing…" + polls
}

function resetRecUI() {
  recording = false;
  if (timerId) { clearInterval(timerId); timerId = null; }
  recBtn.textContent = "● Record";
  recBtn.classList.remove("recording");
  recBtn.disabled = false;
  recName.disabled = false;
  recTimer.classList.add("hidden");
}

recBtn.onclick = () => (recording ? stopRecording() : startRecording());

player.addEventListener("ended", () =>
  document.querySelectorAll(".cite.playing").forEach((c) => c.classList.remove("playing")));

// ---- boot -----------------------------------------------------------------
loadMeetings();
setInterval(loadMeetings, 8000);  // keep history fresh (state changes, new files)
