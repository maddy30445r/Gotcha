#!/usr/bin/env python3
"""
Gotcha — multi-user backend (FastAPI)
=====================================
Holds the API keys, runs the EXISTING pipeline (no reimplementation), and stores
each user's reports/audio in their own namespace. Capture now happens on the
client (the macOS app records two WAVs and uploads them); this server no longer
records anything itself.

  • auth         → Bearer token per user (users.json / GOTCHA_DEV_TOKEN)
  • upload       → POST /api/upload (two WAVs) → validate → cap-check → enqueue
  • transcribe   → pipeline.transcribe_two_track(..., cfg=<per-user>)
  • interpret    → pipeline._interpret_and_save(..., cfg=<per-user>)
  • history/report/audio → per-user files under pipeline_output/<uid>, recordings/<uid>
  • metering     → usage/<uid>.json billed-seconds ledger + a hard per-user cap

Run (local dev):
    GOTCHA_DEV_TOKEN=dev uvicorn webapp.server:app --port 8000

Privacy/security: terminate TLS at the host (encryption in transit); encryption
at rest = an encrypted disk/volume at deploy (FileVault / cloud encrypted volume),
not app-level crypto, so playback stays a plain file serve.
"""

import os
import re
import json
import time
import wave
import queue
import threading

from fastapi import FastAPI, HTTPException, Header, Depends, UploadFile, File, Form
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import markdown as md

import pipeline
from mixdown import mix_tracks

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
STATIC_DIR = os.path.join(HERE, "static")
# Data lives under GOTCHA_DATA_DIR (defaults to the repo root) so a deploy can
# point storage at an encrypted volume and tests at a throwaway dir.
DATA_ROOT = os.environ.get("GOTCHA_DATA_DIR", ROOT)
OUT_ROOT = os.path.join(DATA_ROOT, "pipeline_output")
REC_ROOT = os.path.join(DATA_ROOT, "recordings")
USAGE_DIR = os.path.join(DATA_ROOT, "usage")
USERS_FILE = os.environ.get("GOTCHA_USERS_FILE", os.path.join(ROOT, "users.json"))

MAX_UPLOAD_BYTES = int(os.environ.get("GOTCHA_MAX_UPLOAD_MB", "300")) * 1024 * 1024
DEFAULT_CAP_MIN = float(os.environ.get("GOTCHA_DEFAULT_CAP_MIN", "120"))

# Turn a transcript citation like "[33.84s]" into a clickable span (raw markdown;
# python-markdown passes the inline HTML through).
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
# Users / auth — one opaque bearer token per user. Enough to stop an open paid
# relay; no signup/OAuth in alpha.
# ---------------------------------------------------------------------------
def _load_users():
    """token -> user record {user_id, display_name, cap_minutes?, glossary?,
    hotwords?, provider?, model?}. Loaded once at startup from users.json, plus an
    optional GOTCHA_DEV_TOKEN dev user so local curl testing needs no file."""
    users = {}
    if os.path.exists(USERS_FILE):
        with open(USERS_FILE, encoding="utf-8") as f:
            users = json.load(f)
    dev = os.environ.get("GOTCHA_DEV_TOKEN")
    if dev:
        users.setdefault(dev, {
            "user_id": "dev",
            "display_name": os.environ.get("GOTCHA_YOUR_NAME", "Madhur"),
        })
    return users


USERS = _load_users()


def _user_for_token(token):
    user = USERS.get(token)
    if not user:
        raise HTTPException(403, "Invalid token")
    return user


def auth(authorization: str = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing or malformed Authorization header")
    return _user_for_token(authorization.split(" ", 1)[1].strip())


def _uid(user):
    return user["user_id"]


def _cfg_for(user):
    """Build this user's per-run pipeline config (name + glossary + LLM)."""
    return pipeline.UserConfig(
        your_name=user.get("display_name", pipeline.DEFAULT_CONFIG.your_name),
        glossary=user.get("glossary", list(pipeline.DEFAULT_GLOSSARY)),
        hotwords=user.get("hotwords", list(pipeline.DEFAULT_HOTWORDS)),
        provider=user.get("provider", pipeline.DEFAULT_CONFIG.provider),
        model=user.get("model", pipeline.DEFAULT_CONFIG.model),
    )


# ---------------------------------------------------------------------------
# Per-user storage paths
# ---------------------------------------------------------------------------
def _out_dir(user):
    d = os.path.join(OUT_ROOT, _uid(user))
    os.makedirs(d, exist_ok=True)
    return d


def _rec_dir(user):
    d = os.path.join(REC_ROOT, _uid(user))
    os.makedirs(d, exist_ok=True)
    return d


def _rec_path(user, base, suffix):
    return os.path.join(_rec_dir(user), base + suffix)


def _safe_base(base):
    """Path-traversal guard: a stored base is a single path component."""
    return os.path.basename(base)


_base_lock = threading.Lock()


def _new_base(user, name):
    """Server-generated, collision-safe base within the user's namespace."""
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_") or "meeting"
    stamp = time.strftime("%Y%m%d_%H%M%S")
    rec = _rec_dir(user)
    with _base_lock:
        cand, n = f"{stamp}_{safe}", 1
        while os.path.exists(os.path.join(rec, cand + ".system.wav")):
            n += 1
            cand = f"{stamp}_{safe}-{n}"
        return cand


# ---------------------------------------------------------------------------
# Usage ledger + cost cap (billed seconds = full system + full mic, a
# conservative upper bound on what Sarvam charges since the mic is VAD-trimmed).
# ---------------------------------------------------------------------------
_usage_lock = threading.Lock()


def _ledger_path(user):
    return os.path.join(USAGE_DIR, f"{_uid(user)}.json")


def _read_ledger(user):
    p = _ledger_path(user)
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"used_seconds": 0.0, "meetings": 0}


def _used_min(user):
    return _read_ledger(user).get("used_seconds", 0.0) / 60.0


def _cap_min(user):
    return float(user.get("cap_minutes", DEFAULT_CAP_MIN))


def _usage_add(user, seconds):
    os.makedirs(USAGE_DIR, exist_ok=True)
    with _usage_lock:
        led = _read_ledger(user)
        led["used_seconds"] = led.get("used_seconds", 0.0) + seconds
        led["meetings"] = led.get("meetings", 0) + 1
        with open(_ledger_path(user), "w", encoding="utf-8") as f:
            json.dump(led, f)


def _wav_seconds(path):
    with wave.open(path, "rb") as w:
        return w.getnframes() / float(w.getframerate())


# ---------------------------------------------------------------------------
# Job registry + single background worker (one paid pipeline run at a time —
# the natural cost chokepoint). Keyed by (uid, base) so users don't collide.
# ---------------------------------------------------------------------------
_jobs = {}
_jobs_lock = threading.Lock()
_work_q = queue.Queue()


def _set_state(user, base, state, error=None):
    with _jobs_lock:
        # ts lets a just-queued meeting (no report file yet) sort by recency.
        _jobs[(_uid(user), base)] = {"state": state, "error": error, "ts": time.time()}


def _get_state(user, base):
    with _jobs_lock:
        return dict(_jobs.get((_uid(user), base),
                              {"state": "unknown", "error": None, "ts": 0.0}))


def _ensure_mix(user, base):
    """Cached {base}.mix.wav for combined playback; None if both sources absent."""
    mix_path = _rec_path(user, base, ".mix.wav")
    if os.path.exists(mix_path):
        return mix_path
    sysp, micp = _rec_path(user, base, ".system.wav"), _rec_path(user, base, ".mic.wav")
    if os.path.exists(sysp) and os.path.exists(micp):
        return mix_tracks(sysp, micp, mix_path)
    return None


def _worker():
    while True:
        user, base, system_path, mic_path = _work_q.get()
        cfg = _cfg_for(user)
        try:
            # Re-check the cap at dequeue — a user can queue several uploads
            # before any of them bill, so the enqueue-time check isn't enough.
            billed_min = (_wav_seconds(system_path) + _wav_seconds(mic_path)) / 60.0
            if _used_min(user) + billed_min > _cap_min(user):
                _set_state(user, base, "error", "Usage cap reached — not transcribed.")
                continue

            _set_state(user, base, "transcribing")
            entries = pipeline.transcribe_two_track(system_path, mic_path, cfg=cfg)
            if not entries:
                _set_state(user, base, "error", "Transcription produced no segments.")
                continue

            # Persist transcript BEFORE interpret (keeps the free re-interpret
            # guarantee) and record billed usage now that Sarvam has run.
            out_dir = _out_dir(user)
            with open(os.path.join(out_dir, f"{base}.transcript.json"), "w",
                      encoding="utf-8") as f:
                json.dump(entries, f, ensure_ascii=False, indent=2)
            _usage_add(user, _wav_seconds(system_path) + _wav_seconds(mic_path))

            try:
                _ensure_mix(user, base)
            except Exception as ex:
                print(f"[mix] skipped for {base}: {ex}")

            _set_state(user, base, "interpreting")
            pipeline._interpret_and_save(entries, True, out_dir, base, cfg=cfg)
            _set_state(user, base, "done")
        except BaseException as ex:  # incl. SystemExit (pipeline calls sys.exit)
            _set_state(user, base, "error", str(ex) or repr(ex))
        finally:
            _work_q.task_done()


threading.Thread(target=_worker, daemon=True).start()
app = FastAPI(title="Gotcha")

# The desktop app's webview is a different origin (tauri://localhost) from the
# backend, so the browser preflights cross-origin API calls. Auth is a Bearer
# header (not cookies), so a wildcard origin is safe here.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Meetings / report (all per-user, all behind auth)
# ---------------------------------------------------------------------------
def _tracks_for(user, base):
    out = {}
    for track, suffix in (("system", ".system.wav"), ("mic", ".mic.wav")):
        if os.path.exists(_rec_path(user, base, suffix)):
            out[track] = True
    if "system" in out and "mic" in out:
        out["mix"] = True  # generated lazily
    return out


@app.get("/api/meetings")
def list_meetings(user=Depends(auth)):
    out_dir = _out_dir(user)
    seen = {}
    for fn in os.listdir(out_dir):
        for suffix in (".report.md", ".transcript.json"):
            if fn.endswith(suffix):
                base = fn[: -len(suffix)]
                e = seen.setdefault(base, {"base": base, "mtime": 0.0})
                e["mtime"] = max(e["mtime"], os.path.getmtime(os.path.join(out_dir, fn)))
    # Merge in in-flight jobs (no files yet) and let their job timestamp drive
    # ordering, so a just-recorded meeting sorts to the top immediately.
    with _jobs_lock:
        for (uid, base), j in _jobs.items():
            if uid == _uid(user):
                e = seen.setdefault(base, {"base": base, "mtime": 0.0})
                e["mtime"] = max(e["mtime"], j.get("ts", 0.0))

    meetings = []
    for base, e in seen.items():
        meetings.append({
            "base": base,
            "mtime": e["mtime"],
            "has_report": os.path.exists(os.path.join(out_dir, f"{base}.report.md")),
            "has_transcript": os.path.exists(os.path.join(out_dir, f"{base}.transcript.json")),
            "tracks": _tracks_for(user, base),
            "state": _get_state(user, base)["state"],
        })
    meetings.sort(key=lambda m: m["mtime"], reverse=True)
    return {
        "meetings": meetings,
        "usage": {"used_min": round(_used_min(user), 1), "cap_min": _cap_min(user)},
    }


@app.get("/api/meetings/{base}")
def get_meeting(base: str, user=Depends(auth)):
    base = _safe_base(base)
    out_dir = _out_dir(user)
    report_path = os.path.join(out_dir, f"{base}.report.md")
    transcript_path = os.path.join(out_dir, f"{base}.transcript.json")

    report_html = report_md = None
    if os.path.exists(report_path):
        with open(report_path, encoding="utf-8") as f:
            report_md = f.read()
        report_html = _render_report(report_md)

    transcript = None
    if os.path.exists(transcript_path):
        with open(transcript_path, encoding="utf-8") as f:
            transcript = json.load(f)

    state = _get_state(user, base)
    # 404 only when there's truly nothing — no artifacts AND no live job. An
    # in-flight meeting (queued/transcribing/…) returns 200 so the UI can show
    # "working on it" and poll, instead of a misleading "no artifacts".
    if report_html is None and transcript is None and state["state"] == "unknown":
        raise HTTPException(404, f"No artifacts for {base}")

    return {
        "base": base,
        "report_md": report_md,
        "report_html": report_html,
        "transcript": transcript,
        "tracks": _tracks_for(user, base),
        "your_name": _cfg_for(user).your_name,
        "state": state["state"],
        "error": state["error"],
    }


@app.get("/api/jobs/{base}")
def job_status(base: str, user=Depends(auth)):
    base = _safe_base(base)
    return {"base": base, **_get_state(user, base)}


@app.get("/api/audio/{base}/{track}")
def get_audio(base: str, track: str, token: str = None, authorization: str = Header(None)):
    # An <audio> element can't send an Authorization header, so this endpoint also
    # accepts the token as a query param (?token=...). Header wins when present.
    if authorization and authorization.startswith("Bearer "):
        user = _user_for_token(authorization.split(" ", 1)[1].strip())
    elif token:
        user = _user_for_token(token)
    else:
        raise HTTPException(401, "Missing token")
    base = _safe_base(base)
    if track == "mix":
        try:
            path = _ensure_mix(user, base)
        except Exception as ex:
            raise HTTPException(500, f"Could not build mix: {ex}")
        if not path:
            raise HTTPException(404, f"No two-track audio to mix for {base}")
        return FileResponse(path, media_type="audio/wav")

    suffix = {"system": ".system.wav", "mic": ".mic.wav"}.get(track)
    if not suffix:
        raise HTTPException(400, f"Unknown track: {track}")
    path = _rec_path(user, base, suffix)
    if not os.path.exists(path):
        raise HTTPException(404, f"No {track} audio for {base}")
    # FileResponse serves HTTP Range requests, so <audio> seeking works.
    return FileResponse(path, media_type="audio/wav")


# ---------------------------------------------------------------------------
# Upload — the client records the two tracks and POSTs them here.
# ---------------------------------------------------------------------------
async def _save_upload(upload: UploadFile, dest: str):
    """Stream to disk with a hard size cap, then validate it's a real WAV.
    Returns the audio duration in seconds. Raises HTTPException on bad input."""
    size = 0
    with open(dest, "wb") as f:
        while True:
            chunk = await upload.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_UPLOAD_BYTES:
                f.close()
                os.remove(dest)
                raise HTTPException(413, "Upload exceeds size limit")
            f.write(chunk)
    try:
        return _wav_seconds(dest)  # also validates RIFF/WAVE; pay-per-second guard
    except Exception:
        if os.path.exists(dest):
            os.remove(dest)
        raise HTTPException(400, f"{upload.filename!r} is not a valid WAV file")


@app.post("/api/upload")
async def upload(system: UploadFile = File(...), mic: UploadFile = File(...),
                 name: str = Form("meeting"), user=Depends(auth)):
    # Reject up front if the user is already over cap (cheap fail before writing).
    if _used_min(user) >= _cap_min(user):
        raise HTTPException(429, "Usage cap reached")

    base = _new_base(user, name)
    system_path = _rec_path(user, base, ".system.wav")
    mic_path = _rec_path(user, base, ".mic.wav")
    sys_secs = await _save_upload(system, system_path)
    mic_secs = await _save_upload(mic, mic_path)

    # Cap check against this meeting's (conservative) billed minutes.
    if _used_min(user) + (sys_secs + mic_secs) / 60.0 > _cap_min(user):
        for p in (system_path, mic_path):
            if os.path.exists(p):
                os.remove(p)
        raise HTTPException(429, "This meeting would exceed your usage cap")

    _set_state(user, base, "queued")
    _work_q.put((user, base, system_path, mic_path))
    return {"base": base}


# ---------------------------------------------------------------------------
# Static front-end. Mounted at the ROOT (not /static) so asset paths are
# relative and work identically here and inside the Tauri app (which serves this
# same dir from its root). Mounted LAST so the /api/* routes above win;
# html=True serves index.html at "/".
# ---------------------------------------------------------------------------
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
