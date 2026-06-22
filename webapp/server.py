#!/usr/bin/env python3
"""
Meeting Decoder — local web UI backend (FastAPI)
================================================
A thin server that wraps the EXISTING pipeline (no reimplementation):

  • record control  → recorder_session.RecorderSession (drives the Swift recorder)
  • transcribe      → pipeline.transcribe_two_track / pipeline.transcribe
  • interpret + save → pipeline._interpret_and_save  (writes {base}.report.md)
  • history + report → reads pipeline_output/{base}.{report.md,transcript.json}
  • audio playback   → serves recordings/{base}.{system,mic}.wav with HTTP Range

Run:
    uvicorn webapp.server:app --port 8000     # then open http://localhost:8000

Single-user, localhost-only tool: state (recorder + job registry) is in-memory.
"""

import os
import re
import json
import queue
import threading

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import markdown as md

import pipeline
from recorder_session import RecorderSession, RecorderError
from mixdown import mix_tracks

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
STATIC_DIR = os.path.join(HERE, "static")
OUT_DIR = os.path.join(ROOT, "pipeline_output")
RECORDINGS_DIR = os.path.join(ROOT, "recordings")

# Turn a transcript citation like "[33.84s]" or "[19.87 s]" into a clickable span.
# Done on the raw markdown; python-markdown passes the inline HTML through.
CITE_RE = re.compile(r"\[(\d+(?:\.\d+)?)\s*s\]")


def _linkify_citations(md_text):
    def repl(m):
        ts = m.group(1)
        return f'<span class="cite" data-ts="{ts}">[{ts}s ▶]</span>'
    return CITE_RE.sub(repl, md_text)


def _render_report(md_text):
    html = _linkify_citations(md_text)
    return md.markdown(html, extensions=["extra", "sane_lists", "nl2br"])


# ---------------------------------------------------------------------------
# Job registry + single background worker (one paid pipeline run at a time).
# ---------------------------------------------------------------------------
_jobs = {}                 # base -> {"state": str, "error": str|None}
_jobs_lock = threading.Lock()
_work_q = queue.Queue()


def _set_state(base, state, error=None):
    with _jobs_lock:
        _jobs[base] = {"state": state, "error": error}


def _get_state(base):
    with _jobs_lock:
        return dict(_jobs.get(base, {"state": "unknown", "error": None}))


def _worker():
    while True:
        base, system_path, mic_path = _work_q.get()
        try:
            _set_state(base, "transcribing")
            entries = pipeline.transcribe_two_track(system_path, mic_path)
            if not entries:
                _set_state(base, "error", "Transcription produced no segments.")
                continue

            # Persist the transcript BEFORE interpreting — same guarantee as the
            # CLI: the paid Sarvam result survives a Gemini outage and can be
            # re-interpreted for free.
            os.makedirs(OUT_DIR, exist_ok=True)
            with open(os.path.join(OUT_DIR, f"{base}.transcript.json"), "w",
                      encoding="utf-8") as f:
                json.dump(entries, f, ensure_ascii=False, indent=2)

            # Pre-build the combined playback track so it's ready on first click.
            # Best-effort: a mix failure must never fail the (paid) pipeline run.
            try:
                _ensure_mix(base)
            except Exception as ex:
                print(f"[mix] skipped for {base}: {ex}")

            _set_state(base, "interpreting")
            pipeline._interpret_and_save(entries, True, OUT_DIR, base)
            _set_state(base, "done")
        except BaseException as ex:  # incl. SystemExit (pipeline calls sys.exit)
            _set_state(base, "error", str(ex) or repr(ex))
        finally:
            _work_q.task_done()


threading.Thread(target=_worker, daemon=True).start()
recorder = RecorderSession()
app = FastAPI(title="Meeting Decoder")


# ---------------------------------------------------------------------------
# Meetings / report
# ---------------------------------------------------------------------------
def _rec_path(base, suffix):
    return os.path.join(RECORDINGS_DIR, base + suffix)


def _ensure_mix(base):
    """Return the cached {base}.mix.wav path, generating it from the two source
    tracks if needed. Returns None if the sources aren't both present."""
    mix_path = _rec_path(base, ".mix.wav")
    if os.path.exists(mix_path):
        return mix_path
    system_path, mic_path = _rec_path(base, ".system.wav"), _rec_path(base, ".mic.wav")
    if os.path.exists(system_path) and os.path.exists(mic_path):
        return mix_tracks(system_path, mic_path, mix_path)
    return None


def _tracks_for(base):
    out = {}
    for track, suffix in (("system", ".system.wav"), ("mic", ".mic.wav"),
                          ("single", ".wav")):
        if os.path.exists(_rec_path(base, suffix)):
            out[track] = True
    # A mix is available whenever both two-track sources exist (generated lazily).
    if "system" in out and "mic" in out:
        out["mix"] = True
    return out


@app.get("/api/meetings")
def list_meetings():
    seen = {}
    if os.path.isdir(OUT_DIR):
        for fn in os.listdir(OUT_DIR):
            for suffix in (".report.md", ".transcript.json"):
                if fn.endswith(suffix):
                    base = fn[: -len(suffix)]
                    e = seen.setdefault(base, {"base": base, "mtime": 0.0})
                    e["mtime"] = max(e["mtime"], os.path.getmtime(os.path.join(OUT_DIR, fn)))
    # Merge in jobs (covers in-flight meetings with no files yet).
    with _jobs_lock:
        for base, j in _jobs.items():
            seen.setdefault(base, {"base": base, "mtime": 0.0})

    meetings = []
    for base, e in seen.items():
        state = _get_state(base)["state"]
        meetings.append({
            "base": base,
            "mtime": e["mtime"],
            "has_report": os.path.exists(os.path.join(OUT_DIR, f"{base}.report.md")),
            "has_transcript": os.path.exists(os.path.join(OUT_DIR, f"{base}.transcript.json")),
            "tracks": _tracks_for(base),
            "state": state,
        })
    meetings.sort(key=lambda m: m["mtime"], reverse=True)
    return {"meetings": meetings}


@app.get("/api/meetings/{base}")
def get_meeting(base: str):
    base = os.path.basename(base)  # path-traversal guard
    report_path = os.path.join(OUT_DIR, f"{base}.report.md")
    transcript_path = os.path.join(OUT_DIR, f"{base}.transcript.json")

    report_html = None
    if os.path.exists(report_path):
        with open(report_path, encoding="utf-8") as f:
            report_html = _render_report(f.read())

    transcript = None
    if os.path.exists(transcript_path):
        with open(transcript_path, encoding="utf-8") as f:
            transcript = json.load(f)

    if report_html is None and transcript is None:
        raise HTTPException(404, f"No artifacts for {base}")

    return {
        "base": base,
        "report_html": report_html,
        "transcript": transcript,
        "tracks": _tracks_for(base),
        "your_name": pipeline.YOUR_NAME,
        "state": _get_state(base)["state"],
        "error": _get_state(base)["error"],
    }


@app.get("/api/jobs/{base}")
def job_status(base: str):
    base = os.path.basename(base)
    return {"base": base, **_get_state(base)}


@app.get("/api/audio/{base}/{track}")
def get_audio(base: str, track: str):
    base = os.path.basename(base)
    if track == "mix":
        # Combined playback track — generated + cached on first request so you
        # hear both you and them at the cited moment.
        try:
            path = _ensure_mix(base)
        except Exception as ex:
            raise HTTPException(500, f"Could not build mix: {ex}")
        if not path:
            raise HTTPException(404, f"No two-track audio to mix for {base}")
        return FileResponse(path, media_type="audio/wav")

    suffix = {"system": ".system.wav", "mic": ".mic.wav", "single": ".wav"}.get(track)
    if not suffix:
        raise HTTPException(400, f"Unknown track: {track}")
    path = _rec_path(base, suffix)
    if not os.path.exists(path):
        raise HTTPException(404, f"No {track} audio for {base}")
    # FileResponse serves HTTP Range requests, so <audio> seeking works.
    return FileResponse(path, media_type="audio/wav")


# ---------------------------------------------------------------------------
# Record control
# ---------------------------------------------------------------------------
class StartBody(BaseModel):
    name: str = "meeting"
    mic: str | None = None


@app.post("/api/record/start")
def record_start(body: StartBody):
    try:
        base = recorder.start(name=body.name, mic_uid=body.mic)
    except RecorderError as ex:
        raise HTTPException(409, str(ex))
    _set_state(base, "recording")
    return {"base": base, "recording": True}


@app.post("/api/record/stop")
def record_stop():
    try:
        base, system_path, mic_path = recorder.stop()
    except RecorderError as ex:
        raise HTTPException(400, str(ex))
    if not (os.path.exists(system_path) and os.path.exists(mic_path)):
        _set_state(base, "error", "Recording stopped but the WAV files are missing.")
        raise HTTPException(500, "Recording produced no audio files.")
    _set_state(base, "queued")
    _work_q.put((base, system_path, mic_path))
    return {"base": base}


@app.get("/api/record/status")
def record_status():
    return recorder.status()


# ---------------------------------------------------------------------------
# Static front-end
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def index():
    with open(os.path.join(STATIC_DIR, "index.html"), encoding="utf-8") as f:
        return f.read()


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
