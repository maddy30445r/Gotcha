# Handoff — meeting decoder

_Last updated: 2026-06-23 (web UI session)_

## What this is
Personal tool that decodes Hinglish dev standups for "Madhur": record → Sarvam
transcribes (codemix + diarization + timestamps) → Gemini/Opus interprets into a
personalized, cited report (what was decided, what the lead meant, your action
items). Heading toward a shippable macOS app; the Swift capture core is the
reusable piece.

## Architecture (stable)
- **Capture** = ScreenCaptureKit, **two separate tracks** (no mixing):
  - system audio (the call / others) via `SCStream` → `*.system.wav`
  - mic (you) via AVAudioEngine **Voice Processing** → `*.mic.wav`
  - VP cancels the call audio bleeding into the mic on speakers → **works without
    headphones**. Validated (raw mic rms ~1029 → VP mic ~60).
- **Transcription** = 2 Sarvam jobs: system diarized (relabeled `Other`/`Other-N`),
  mic as single known speaker (`force_speaker=Madhur`), VAD-trimmed to cut cost.
  Merged into one timeline by timestamp → reliable you-vs-them labels.
- Full reasoning in memory: `decision-two-track-audio`, `reference-sarvam-stt`,
  `project-meeting-decoder`.

## Files
- `mac_recorder/` — Swift package (`MeetingCaptureKit` lib + `mac-recorder` CLI).
  - `MeetingRecorder.swift` — SCStream system + VP mic, started in that order.
  - `VoiceProcessingMic.swift` — VP mic, extracts channel 0 (the clean mic).
  - `main.swift` — CLI: `--out-system`/`--out-mic`/`--mic`/`--list-mics`.
- `record_meeting.py` — CLI launcher; rebuilds Swift if stale; writes the two WAVs.
  **Untouched this session** (keep the validated CLI flow intact for the real-call test).
- `pipeline.py` — transcribe + interpret.
- `vad.py` — mic silence-trim + timestamp remap (0.7s gaps keep utterances separate).
- **`webapp/`** — local web UI (this session). `server.py` (FastAPI) imports the
  `pipeline.py` functions directly (no reimpl) + `static/{index.html,app.js,style.css}`
  (vanilla, no build step). Run: `python3 -m uvicorn webapp.server:app --port 8000`.
- **`recorder_session.py`** — non-blocking start/stop wrapper around `mac-recorder` for
  the web server; **reuses** `record_meeting.py`'s `ensure_built`/`BINARY`/`_request_stop`/
  `_report_failure` + the "RECORDING" handshake (one source of truth, CLI not duplicated).
- **`mixdown.py`** — sums the two tracks into `{base}.mix.wav` for *playback only* (stdlib
  `wave`+`array`, not `audioop` which is gone in 3.13). Does NOT touch transcription.

## Done this session (web UI — v1)
Built a **local web app** so the tool is daily-usable without the CLI. Form factor chosen
to be light + cross-OS: a browser UI; the macOS-only capture stays behind the Swift CLI.
- **Backend** (`webapp/server.py`, FastAPI): imports `pipeline.py` directly. Endpoints —
  `GET /api/meetings` (history + state), `GET /api/meetings/{base}` (report rendered to
  HTML with `[33.84s]` citations turned into clickable chips + transcript + tracks),
  `GET /api/audio/{base}/{track}` (serves WAVs with HTTP **Range** so `<audio>` seeks),
  `POST /api/record/{start,stop}`, `GET /api/record/status`, `GET /api/jobs/{base}`.
  One background worker mirrors the CLI two-track path and **saves the transcript before
  interpret** (keeps the "Gemini outage = free re-interpret" guarantee); it catches
  `SystemExit` (pipeline calls `sys.exit`) so failures surface in the UI, not silently.
- **Frontend** (`webapp/static/`): Record button + live timer, history sidebar with status
  badges, rendered report, sticky audio player. Click a citation → audio seeks to that
  second.
- **Combined playback (`mixdown.py`)**: citations default to a mixed `{base}.mix.wav` so
  you hear BOTH sides of the moment; generated lazily on first request + cached, pre-built
  by the worker. A `[ Mix | You | Them ]` selector isolates a side (You=mic, Them=system).
- **Verified** (offline, against the existing sample meeting): meetings list, report render
  (3 sections, Hinglish intact, 4 citation chips), audio Range → `206 Partial Content`,
  mix lazy-gen + cache + range, path-traversal collapses to 404. Record control + a full
  paid pipeline run still need the real-call test (below).

### KNOWN: old recordings sound echoey in Mix — expected, not a bug
The Jun 22 sample's **mic track has system audio bled into it** (verified: when others are
loud and you're silent, mic RMS ~2866 vs a ~200 silent floor — ~10× the floor, tracking
the system not your voice; it's reverberant/VP-processed so it's NOT linearly correlated,
which is why a waveform-correlation test missed it). So that recording did **not** get
effective echo cancellation (predates/!VP). Result: You = your voice + bled call audio;
Mix = system doubled (delayed) = echo; Them = clean. The mix code is correct — a fresh
**VP** recording should have a clean mic (memory: bleed RMS ~1029→~60) → clean mix.

## Earlier session (pipeline hardening)
- Removed dead AEC-experiment code. **Parallelized the 2 Sarvam jobs** (ThreadPoolExecutor,
  ~half wall-clock). **Free re-interpretation** from a saved `transcript.json`.
  **Partial-failure resilience** (`_settle` keeps one track if the other dies; transcript
  saved before interpret). All offline tests passed.

## OPEN — needs the user (one real-call test, now covers two things)
Record a **real speaker-mode meeting** (Discord/Meet, no headphones). Easiest via the UI:
```
python3 -m uvicorn webapp.server:app --port 8000   # open http://localhost:8000
# click ● Record → talk on speakers → ■ Stop → watch it process → read the report
```
(or the CLI: `python3 record_meeting.py --name discord_nohp` then `python3 pipeline.py
recordings/<stamp>_discord_nohp.system.wav recordings/<stamp>_discord_nohp.mic.wav`.)
This single test validates BOTH still-open items:
1. **No-headphones VP fix**: your `Madhur` transcript entries are ONLY your lines (no
   duplicate twins of others' speech), VAD keep-% ~20-30%.
2. **Clean mic ⇒ clean Mix**: in the UI, a citation on **Mix** should have NO echo (the
   mic must be free of system bleed — see the KNOWN note above). Sanity-check: mic RMS in
   an others-talking / you-silent window should be near the noise floor, not ~half system.
Note: the server spawns the recorder, so **Mic + Screen-Recording permission must be on the
app running uvicorn** (your terminal) — same one-time grant as the CLI.

## Possible next improvements (not started)
- **Re-interpret button** in the UI (trivial: `/api/reinterpret/{base}` → `_load_saved_
  transcript` + `_interpret_and_save`) — exposes the existing free re-interpret path.
- Externalize config (YOUR_NAME / GLOSSARY / HOTWORDS / GEMINI_MODEL → .env/config file);
  the UI could then edit YOUR_NAME instead of it being hard-coded in `pipeline.py`.
- Web UI polish: live waveform/level meter while recording, delete-meeting, mic picker
  (`--list-mics` is already wired in `recorder_session.start(mic_uid=…)`), clip-region
  highlight on the player.
- Eventual macOS app shell linking MeetingCaptureKit (the report/history/playback UX from
  this web UI carries over).
- Deps added this session: `fastapi`, `uvicorn`, `markdown` (in `requirements.txt`).
