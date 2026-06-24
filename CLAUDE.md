# Gotcha — project guide for Claude

> **Gotcha** — *Just say Gotcha in your meetings and offload it to us.*
> A friendly in-meeting advisor for developers: it decodes the hard parts of a meeting
> (what was decided, what the lead really meant, your action items) and points you to the
> exact moment each claim came from — so you stop guessing or re-prompting after standups.

## Goal
Turn the existing personal "meeting decoder" into a **shippable product, Gotcha**:
- **macOS first, cross-OS-ready** (Windows later via one new capture module).
- **External users**, **managed keys** (we pay Sarvam + LLM, bill later).
- Near-term milestone: **alpha for 3–5 real users** on their own Macs.
- Full productization plan: `~/.claude/plans/okay-read-the-handoff-snoopy-shannon.md`.

## Project flow (end to end)
1. **Capture** (macOS-local, the crown jewel) — ScreenCaptureKit records **two separate
   tracks**: `*.system.wav` (the call/others) + `*.mic.wav` (you, via AVAudioEngine Voice
   Processing, which cancels speaker bleed so it works **without headphones**). No mixing.
2. **Transcribe** — 2 Sarvam jobs (`saaras:v3`, codemix + diarization + timestamps): system
   track diarized (`Other`/`Other-N`), mic track forced to one known speaker + VAD-trimmed
   to cut cost. Merged into one timeline by timestamp → reliable you-vs-them labels.
3. **Interpret** — an LLM turns the transcript into a personalized, **citation-backed**
   report (decisions / lead-decode / your action items); every claim cites a `[timestamp]`.
   Transcript is saved **before** interpret, so a re-interpret is free.
4. **Present** — report + history + audio player; clicking a `[timestamp]` seeks the audio
   to that second. `[Mix|You|Them]` selector for playback only (`mixdown.py`).

## Target architecture (anti-lock-in — the productization)
The only OS-specific code is a thin capture sidecar; everything else is shared.
- **Backend (cloud, shared)** — holds keys, runs `pipeline.py` (transcribe + interpret),
  stores per-user reports/audio (encrypted), auth, usage metering + cost cap.
- **Web UI (shared)** — `webapp/static/` is the canonical surface (report/history/player).
- **Tauri shell (shared, Mac & Windows identical)** — hosts the web UI, spawns the sidecar.
- **Capture sidecar (only OS-specific part)** — macOS = the Swift `mac-recorder` binary;
  Windows later = a WASAPI binary honoring the **same contract**: produce the two WAVs.
- **The one seam:** "two WAVs + auth token → `POST /api/upload`." Windows = new sidecar only.

## Key files
- `mac_recorder/` — Swift `MeetingCaptureKit` lib + `mac-recorder` CLI (the capture core).
- `pipeline.py` — `transcribe_two_track()` + `interpret()`. **Has hard-coded config**
  (`YOUR_NAME`, `GLOSSARY`, `HOTWORDS`, model) at the top — being externalized per-user.
- `webapp/server.py` — FastAPI; currently single-user/localhost, becoming the multi-user
  backend (add auth + `/api/upload`, drop the local record endpoints).
- `webapp/static/` — vanilla HTML/JS/CSS UI (no build step).
- `vad.py`, `mixdown.py`, `record_meeting.py`, `recorder_session.py` — capture/processing helpers.
- `HANDOFF.md` — running state notes (gitignored). Open item: validate clean no-headphones
  VP capture on a fresh real call.

## Privacy / cost constraints (don't break these)
- **No-train LLM for real meetings.** Gemini free tier may train on inputs → testing only.
  Real meetings must use a paid/no-train tier (Anthropic API, Vertex, or Gemini paid).
- **Two-track Sarvam ≈ 2× cost** (~$0.40/30-min meeting); VAD-trims the mic to claw it back.
  Always meter usage + enforce a per-user cap before paying Sarvam.
- Keys live in `.env` (`SARVAM_API_KEY`, `GEMINI_API_KEY`) — never commit or expose them.

## Working agreement — keep memory current
This project's durable context lives in the auto-memory at
`~/.claude/projects/-Users-madhurmittal-Desktop-Gotcha/memory/`. **At the end of
any session that changes the project's goal, architecture, stage, or a key decision, update
the relevant memory file (and `MEMORY.md` index) so the next session has full context.**
Record *decisions and why* (not things already obvious from code/git). Current anchors:
`project-meeting-decoder`, `decision-gotcha-productization`, `decision-two-track-audio`,
`reference-sarvam-stt`.
