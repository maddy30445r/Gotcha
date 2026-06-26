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
import html
import json
import time
import wave
import queue
import threading

from urllib.parse import quote, urlparse, urlencode

from fastapi import FastAPI, HTTPException, Header, Depends, UploadFile, File, Form, Body, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import markdown as md

import pipeline
from mixdown import mix_tracks
from . import auth as authmod

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

# Turn a transcript citation like "[33.84s]" (the trailing "s" is optional — the
# LLM is inconsistent) into a clickable span (raw markdown; python-markdown
# passes the inline HTML through).
CITE_RE = re.compile(r"\[(\d+(?:\.\d+)?)\s*s?\]")


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

# Self-serve account store (Milestone 2): create the SQLite DB and, on first run,
# import any legacy users.json so existing minted testers keep working (their old
# token becomes their api_token). users.json stays as an auth fallback.
authmod.init_db()
_migrated = authmod.migrate_users_json(USERS_FILE)
if _migrated:
    print(f"[auth] migrated {_migrated} user(s) from users.json into gotcha.db")


def _user_for_token(token):
    """Resolve a Bearer/API token to a user record — the self-serve DB first, then
    the legacy users.json / GOTCHA_DEV_TOKEN map. A Bearer token means the desktop app,
    so stamp 'desktop seen' (throttled) — that's how the web app learns a Mac app is
    connected."""
    rec = authmod.user_by_api_token(token)
    if rec:
        if time.time() - (rec.get("desktop_seen_at") or 0) > 60:
            authmod.touch_desktop_seen(rec["user_id"])
        return rec
    user = USERS.get(token)
    if not user:
        raise HTTPException(403, "Invalid token")
    return user


def auth(request: Request, authorization: str = Header(None)):
    """A request is authenticated by EITHER a Bearer api_token (desktop / CLI) OR a
    signed session cookie (web). Both resolve to the same user-record shape."""
    if authorization and authorization.startswith("Bearer "):
        return _user_for_token(authorization.split(" ", 1)[1].strip())
    uid = authmod.read_session(request.cookies.get(authmod.SESSION_COOKIE))
    if uid:
        rec = authmod.user_by_id(uid)
        if rec:
            return rec
    raise HTTPException(401, "Not signed in")


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


def _meta_path(user, base):
    return os.path.join(_out_dir(user), f"{base}.meta.json")


def _read_meta(user, base):
    p = _meta_path(user, base)
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _write_meta(user, base, **fields):
    meta = _read_meta(user, base)
    meta.update(fields)
    with open(_meta_path(user, base), "w", encoding="utf-8") as f:
        json.dump(meta, f)


def _display_name(user, base, meta=None):
    """Human label for a meeting: the saved name, else derived from the base id
    (e.g. 20260625_143000_standup-1 → "standup")."""
    meta = meta if meta is not None else _read_meta(user, base)
    nm = (meta.get("name") or "").strip()
    if nm:
        return nm
    m = re.match(r"^\d{8}_\d{6}_(.+?)(?:-\d+)?$", base)
    slug = m.group(1) if m else base
    return slug.replace("-", " ").replace("_", " ").strip() or base


def _resolve_state(user, base):
    """Durable state: prefer the live in-memory job, else infer from disk so a
    'parked' or 'done' meeting survives a server restart (the job registry doesn't)."""
    st = _get_state(user, base)
    if st["state"] != "unknown":
        return st
    out = _out_dir(user)
    if os.path.exists(os.path.join(out, f"{base}.report.md")):
        return {"state": "done", "error": None}
    if _read_meta(user, base).get("parked"):
        return {"state": "parked", "error": None}
    if os.path.exists(os.path.join(out, f"{base}.transcript.json")):
        # Transcript saved but no report → interpret never finished (LLM outage /
        # out of credits / restart mid-interpret). It's free to re-interpret.
        return {"state": "error",
                "error": "Interpretation didn't finish — re-interpret to retry (free)."}
    return {"state": "unknown", "error": None}


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
        user, base, system_path, mic_path, glossary = _work_q.get()
        cfg = _cfg_for(user)
        # Per-meeting glossary terms augment the user's defaults — used both as
        # Sarvam hotwords (source bias) and in the interpret prompt (fix proper nouns).
        if glossary:
            cfg.glossary = list(cfg.glossary) + list(glossary)
            cfg.hotwords = list(cfg.hotwords) + list(glossary)
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


@app.get("/healthz")
def healthz():
    """Unauthenticated liveness probe for the host / uptime checks."""
    return {"ok": True}


WAITLIST_FILE = os.path.join(DATA_ROOT, "waitlist.jsonl")
_waitlist_lock = threading.Lock()


@app.post("/api/request-access")
def request_access(email: str = Body("", embed=True),
                   website: str = Body("", embed=True)):
    """Beta waitlist (unauthenticated): append an email to waitlist.jsonl on the
    data volume. `website` is a honeypot — real users leave it blank, bots fill it,
    so a non-empty value is silently dropped."""
    if website.strip():
        return {"ok": True}
    email = (email or "").strip().lower()
    if len(email) > 200 or not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        raise HTTPException(422, "Enter a valid email")
    line = json.dumps({"email": email, "ts": time.time()}, ensure_ascii=False)
    with _waitlist_lock:
        with open(WAITLIST_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    return {"ok": True}


@app.get("/app")
def app_page():
    """Serve the app shell at a clean /app path (the static mount also serves it at
    /app.html). The marketing landing lives at /."""
    return FileResponse(os.path.join(STATIC_DIR, "app.html"))


@app.get("/login")
def login_page():
    """Self-serve sign-in / sign-up page (Google + email magic-link)."""
    return FileResponse(os.path.join(STATIC_DIR, "login.html"))


DESKTOP_WINDOW = 14 * 24 * 3600  # treat the Mac app as "connected" if seen within 14 days


def _has_desktop(user):
    """Has a desktop app been active for this account recently? Proxy for 'installed'."""
    seen = user.get("desktop_seen_at") or 0
    return bool(seen) and (time.time() - seen) < DESKTOP_WINDOW


@app.get("/api/auth/me")
def auth_me(user=Depends(auth)):
    """Who am I — used by the web app on boot to decide app-vs-login."""
    return {
        "user_id": user["user_id"],
        "email": user.get("email"),
        "display_name": user.get("display_name"),
        "used_min": round(_used_min(user), 1),
        "cap_min": _cap_min(user),
        "has_desktop": _has_desktop(user),
    }


@app.post("/api/auth/logout")
def auth_logout():
    """Clear the web session cookie. (Bearer API tokens are unaffected.)"""
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(authmod.SESSION_COOKIE, path="/")
    return resp


def _base_url(request):
    return authmod.PUBLIC_URL or str(request.base_url).rstrip("/")


def _set_session(resp, user_id):
    resp.set_cookie(
        authmod.SESSION_COOKIE, authmod.make_session(user_id),
        max_age=authmod.SESSION_TTL, httponly=True, samesite="lax",
        secure=authmod.PUBLIC_URL.startswith("https://"), path="/")


def _desktop_connect_page(link):
    """Interstitial for linking the desktop app. It fires the gotcha:// deep link and, once
    the app takes focus (the tab is backgrounded), shows 'You're signed in'. Reaching this
    page means the desktop client started the flow and the token was already delivered, so the
    resting state is 'signed in' with a small get-the-app link — not a misleading 'install the
    app' takeover. (The loopback flow serves its own success page and never hits this.)"""
    href = html.escape(link, quote=True)   # for the HTML attribute (& -> &amp;)
    js = json.dumps(link)                   # a safe JS string literal (keeps & intact)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Connecting · Gotcha</title>
<style>
  body {{ margin:0; min-height:100vh; display:grid; place-items:center;
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
    color:#221d17; background:#f4ecdf;
    background-image:radial-gradient(680px 320px at 50% -6%,#efeafb 0,transparent 70%); }}
  .card {{ max-width:380px; margin:24px; padding:40px 34px; text-align:center;
    background:#fffdf8; border:1px solid #e7ddca; border-radius:20px;
    box-shadow:0 14px 36px -30px rgba(80,60,30,.5); }}
  .card[hidden] {{ display:none; }}
  .mark {{ width:40px; height:40px; margin:0 auto 18px; border-radius:11px;
    background:#5d4ce6; color:#fff; font-weight:800; font-size:22px;
    display:grid; place-items:center; }}
  h1 {{ font-size:22px; font-weight:800; letter-spacing:-.02em; margin:0 0 8px; }}
  p {{ color:#7a7163; font-size:14.5px; line-height:1.55; margin:0 0 22px; }}
  a.btn {{ display:inline-block; padding:11px 20px; border-radius:11px;
    background:#5d4ce6; color:#fff; font-weight:600; font-size:14.5px;
    text-decoration:none; }}
  .alts {{ margin:20px 0 0; display:flex; flex-direction:column; gap:8px; }}
  .alts a {{ color:#7a7163; font-size:12.5px; text-decoration:none; }}
  .alts a:hover {{ color:#5d4ce6; text-decoration:underline; }}
</style></head>
<body>
  <div class="card" id="launching">
    <div class="mark">G</div>
    <h1>Opening Gotcha…</h1>
    <p>If the Mac app is installed, it's opening now — you can head back to it.</p>
    <a class="btn" href="{href}">Open Gotcha</a>
    <div class="alts">
      <a href="/api/auth/desktop/connect?force=1">Not you? Use a different account</a>
    </div>
  </div>

  <div class="card" id="success" hidden>
    <div class="mark">G</div>
    <h1>You're signed in</h1>
    <p>Gotcha is open on your Mac — you can close this tab and head back to the app.</p>
    <div class="alts">
      <a href="{href}">Not back in the app? Open Gotcha</a>
      <a href="/download.html">Don't have it yet? Get the Mac app</a>
    </div>
  </div>

  <script>
  (function () {{
    var link = {js};
    function show(id) {{
      ["launching", "success"].forEach(function (k) {{
        document.getElementById(k).hidden = (k !== id);
      }});
    }}
    // The app taking focus backgrounds this tab → confirms it opened. With the app's
    // window-focus fix this normally fires on its own.
    function onAway() {{ show("success"); }}
    document.addEventListener("visibilitychange", function () {{
      if (document.visibilityState === "hidden") onAway();
    }});
    window.addEventListener("blur", onAway);
    window.addEventListener("pagehide", onAway);
    // Top-level navigation is the reliable launcher on macOS (an iframe gets blocked).
    setTimeout(function () {{ window.location.href = link; }}, 100);
    // Reaching this page means the desktop client started the flow and the token was
    // already delivered — so rest on "signed in" (with a small get-the-app link) rather
    // than a misleading "install the app" screen. ~3.5s allows Chrome's "Open Gotcha?" prompt.
    setTimeout(function () {{
      if (document.visibilityState !== "hidden") show("success");
    }}, 3500);
  }})();
  </script>
</body></html>"""


# --- desktop linking: deep link (scheme) vs loopback (localhost) -------------
# The desktop app can collect its token two ways: the gotcha:// deep link (works when
# the app is closed, but relies on a registered URL scheme), or a loopback redirect —
# the app starts a local 127.0.0.1 server and we redirect the browser there with the
# token (RFC 8252; no scheme, no error dialog). The loopback target rides through the
# OAuth round-trip in a short-lived signed cookie, so no OAuth-state surgery is needed.
DESKTOP_REDIRECT_COOKIE = "gotcha_desktop_redirect"


def _is_loopback(url):
    """True only for an http://127.0.0.1|localhost|[::1][:port]/… URL — refuse anything
    else so a token can never be redirected to an attacker-controlled host."""
    try:
        u = urlparse(url or "")
        return u.scheme == "http" and (u.hostname in ("127.0.0.1", "localhost", "::1"))
    except Exception:
        return False


def _deeplink(request, tok):
    return (f"gotcha://connect?server={quote(_base_url(request))}&token={quote(tok)}")


def _loopback_url(base, tok, state=None):
    q = {"token": tok}
    if state:
        q["state"] = state
    sep = "&" if urlparse(base).query else "?"
    return base + sep + urlencode(q)


def _set_desktop_redirect(resp, redirect, state):
    resp.set_cookie(
        DESKTOP_REDIRECT_COOKIE, authmod.sign_payload({"r": redirect, "s": state or ""}),
        max_age=600, httponly=True, samesite="lax",
        secure=authmod.PUBLIC_URL.startswith("https://"), path="/")


def _clear_desktop_redirect(resp):
    resp.delete_cookie(DESKTOP_REDIRECT_COOKIE, path="/")


def _finish_login(request, email, client, display_name=None):
    """Find-or-create the account (open signup → free cap), then hand the client its
    credential: web gets a session cookie + redirect into the app; desktop gets its
    api_token — via the loopback redirect if one was remembered, else the gotcha:// deep
    link interstitial. Either way the browser is also signed into the web."""
    user, _created = authmod.find_or_create_user(email, display_name=display_name)
    if client == "desktop":
        tok = authmod.api_token_for(user["user_id"])
        lb = authmod.read_payload(request.cookies.get(DESKTOP_REDIRECT_COOKIE))
        if lb and _is_loopback(lb.get("r", "")):
            resp = RedirectResponse(_loopback_url(lb["r"], tok, lb.get("s")), status_code=303)
            _clear_desktop_redirect(resp)
        else:
            resp = HTMLResponse(_desktop_connect_page(_deeplink(request, tok)))
        _set_session(resp, user["user_id"])  # one login also signs this browser into web
        return resp
    resp = RedirectResponse("/app", status_code=303)
    _set_session(resp, user["user_id"])
    return resp


@app.get("/api/auth/desktop/connect")
def desktop_connect(request: Request, force: int = 0, redirect: str = None, state: str = None):
    """Link the desktop app. `redirect` (a loopback URL) selects the loopback flow; absent,
    we fall back to the gotcha:// deep-link interstitial. If this browser already has a web
    session (and not forcing a re-pick), connect that account straight away — no second
    login; otherwise send the user through sign-in, remembering the loopback target."""
    loopback = redirect if _is_loopback(redirect) else None
    if not force:
        uid = authmod.read_session(request.cookies.get(authmod.SESSION_COOKIE))
        rec = authmod.user_by_id(uid) if uid else None
        if rec:
            tok = authmod.api_token_for(rec["user_id"])
            if loopback:
                resp = RedirectResponse(_loopback_url(loopback, tok, state), status_code=303)
                _clear_desktop_redirect(resp)
                return resp
            return HTMLResponse(_desktop_connect_page(_deeplink(request, tok)))
    resp = RedirectResponse("/login?client=desktop", status_code=303)
    if loopback:
        _set_desktop_redirect(resp, loopback, state)
    else:
        _clear_desktop_redirect(resp)  # don't let a stale loopback target linger
    return resp


@app.post("/api/auth/email/start")
def auth_email_start(request: Request, email: str = Body(..., embed=True),
                     client: str = Body("web", embed=True)):
    """Begin email (magic-link) sign-in/up: mint a one-time link and email it."""
    email = (email or "").strip().lower()
    if len(email) > 200 or not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        raise HTTPException(422, "Enter a valid email")
    token = authmod.create_magic_link(email)
    client = "desktop" if client == "desktop" else "web"
    link = f"{_base_url(request)}/api/auth/email/verify?token={token}&client={client}"
    authmod.send_email(
        email, "Your Gotcha sign-in link",
        f'<p>Click to sign in to Gotcha:</p>'
        f'<p><a href="{link}">Sign in to Gotcha</a></p>'
        f"<p>This link expires in 15 minutes. If you didn't request it, ignore this email.</p>")
    return {"ok": True}


@app.get("/api/auth/email/verify")
def auth_email_verify(request: Request, token: str, client: str = "web"):
    """Consume a magic link → sign the user in (web cookie or desktop deep link)."""
    email = authmod.consume_magic_link(token)
    if not email:
        return RedirectResponse("/login?error=expired", status_code=303)
    return _finish_login(request, email, "desktop" if client == "desktop" else "web")


def _google_redirect_uri(request):
    """Must match a redirect URI registered on the Google OAuth client exactly."""
    return _base_url(request) + "/api/auth/google/callback"


@app.get("/api/auth/google/start")
def auth_google_start(request: Request, client: str = "web"):
    """Kick off Google sign-in: redirect to the consent screen."""
    if not authmod.google_enabled():
        return RedirectResponse("/login?error=google_off", status_code=303)
    client = "desktop" if client == "desktop" else "web"
    state = authmod.make_oauth_state(client)
    url = authmod.google_auth_url(_google_redirect_uri(request), state)
    return RedirectResponse(url, status_code=303)


@app.get("/api/auth/google/callback")
def auth_google_callback(request: Request, code: str = None,
                         state: str = None, error: str = None):
    """Google redirects back here with a code → exchange it, sign the user in."""
    if error or not code:
        return RedirectResponse("/login?error=google", status_code=303)
    client = authmod.read_oauth_state(state)
    if client is None:
        return RedirectResponse("/login?error=google", status_code=303)
    email, name = authmod.google_exchange(code, _google_redirect_uri(request))
    if not email:
        return RedirectResponse("/login?error=google", status_code=303)
    return _finish_login(request, email, client, display_name=name)


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
        for suffix in (".report.md", ".transcript.json", ".meta.json"):
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
            "name": _display_name(user, base),
            "created": e["mtime"],
            "mtime": e["mtime"],
            "has_report": os.path.exists(os.path.join(out_dir, f"{base}.report.md")),
            "has_transcript": os.path.exists(os.path.join(out_dir, f"{base}.transcript.json")),
            "tracks": _tracks_for(user, base),
            "state": _resolve_state(user, base)["state"],
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

    state = _resolve_state(user, base)
    # 404 only when there's truly nothing — no artifacts AND no live/parked job.
    # An in-flight or parked meeting returns 200 so the UI can show "working on
    # it" / the catch-up CTA instead of a misleading "no artifacts".
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
def get_audio(base: str, track: str, request: Request,
              token: str = None, authorization: str = Header(None)):
    # An <audio> element can't send an Authorization header, so this endpoint also
    # accepts the token as a query param (desktop) OR the session cookie (web).
    # Header wins, then explicit token, then cookie.
    if authorization and authorization.startswith("Bearer "):
        user = _user_for_token(authorization.split(" ", 1)[1].strip())
    elif token:
        user = _user_for_token(token)
    else:
        uid = authmod.read_session(request.cookies.get(authmod.SESSION_COOKIE))
        user = authmod.user_by_id(uid) if uid else None
        if not user:
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


def _parse_glossary(raw):
    """Split a user-typed glossary blob (commas / semicolons / newlines) into terms."""
    if not raw:
        return []
    return [p.strip() for p in re.split(r"[\n,;]+", raw) if p.strip()]


@app.post("/api/upload")
async def upload(system: UploadFile = File(...), mic: UploadFile = File(...),
                 name: str = Form("meeting"), glossary: str = Form(""),
                 process: str = Form("true"), user=Depends(auth)):
    process_now = str(process).strip().lower() in ("1", "true", "yes", "on")
    terms = _parse_glossary(glossary)

    base = _new_base(user, name)
    system_path = _rec_path(user, base, ".system.wav")
    mic_path = _rec_path(user, base, ".mic.wav")
    sys_secs = await _save_upload(system, system_path)
    mic_secs = await _save_upload(mic, mic_path)

    # Cap is about paid transcription minutes — only relevant when processing now.
    # Parking just stores the audio (no paid work), so it's never blocked by cap;
    # and a "process now" that would bust the cap is parked instead of discarded,
    # so the user never loses a recording.
    over_cap = _used_min(user) + (sys_secs + mic_secs) / 60.0 > _cap_min(user)
    if process_now and not over_cap:
        _write_meta(user, base, glossary=terms, parked=False, name=name)
        _set_state(user, base, "queued")
        _work_q.put((user, base, system_path, mic_path, terms))
        state = "queued"
    else:
        _write_meta(user, base, glossary=terms, parked=True, name=name)
        _set_state(user, base, "parked")
        state = "parked"
    # Return the resulting state so the client can confirm park-vs-process took.
    return {"base": base, "state": state}


@app.post("/api/process/{base}")
def process_meeting(base: str, user=Depends(auth)):
    """Start (or resume) processing a parked meeting whose audio is already stored."""
    base = _safe_base(base)
    system_path = _rec_path(user, base, ".system.wav")
    mic_path = _rec_path(user, base, ".mic.wav")
    if not (os.path.exists(system_path) and os.path.exists(mic_path)):
        raise HTTPException(404, "No recording to process")
    billed_min = (_wav_seconds(system_path) + _wav_seconds(mic_path)) / 60.0
    if _used_min(user) + billed_min > _cap_min(user):
        raise HTTPException(429, "This meeting would exceed your usage cap")
    terms = _read_meta(user, base).get("glossary", [])
    _write_meta(user, base, parked=False)
    _set_state(user, base, "queued")
    _work_q.put((user, base, system_path, mic_path, terms))
    return {"base": base}


def _reinterpret_job(user, base, terms):
    """Re-run ONLY the interpret step on a saved transcript. Free (no Sarvam), so
    it doesn't touch the cap or the Sarvam-serializing work queue — runs in its own
    thread. Recovers a meeting whose interpret failed (e.g. LLM outage / out of
    credits) without re-recording or re-transcribing."""
    cfg = _cfg_for(user)
    if terms:
        cfg.glossary = list(cfg.glossary) + list(terms)
    try:
        _set_state(user, base, "interpreting")
        path = os.path.join(_out_dir(user), f"{base}.transcript.json")
        entries, two_track = pipeline._load_saved_transcript(path, cfg=cfg)
        pipeline._interpret_and_save(entries, two_track, _out_dir(user), base, cfg=cfg)
        _set_state(user, base, "done")
    except BaseException as ex:  # incl. SystemExit (pipeline calls sys.exit)
        _set_state(user, base, "error", str(ex) or repr(ex))


@app.post("/api/reinterpret/{base}")
def reinterpret_meeting(base: str, user=Depends(auth)):
    """Re-interpret an already-transcribed meeting for free (skips Sarvam). Use after
    fixing an interpret failure — out of LLM credits, a bad prompt, a glossary tweak."""
    base = _safe_base(base)
    if not os.path.exists(os.path.join(_out_dir(user), f"{base}.transcript.json")):
        raise HTTPException(404, "No saved transcript to re-interpret")
    terms = _read_meta(user, base).get("glossary", [])
    _set_state(user, base, "interpreting")
    threading.Thread(target=_reinterpret_job, args=(user, base, terms), daemon=True).start()
    return {"base": base, "state": "interpreting"}


@app.patch("/api/meetings/{base}")
def rename_meeting(base: str, name: str = Body(..., embed=True), user=Depends(auth)):
    """Rename a meeting — updates the saved display name only; the base id (and all
    file paths) are unchanged, so audio/report/transcript stay put."""
    base = _safe_base(base)
    name = (name or "").strip()[:120]
    if not name:
        raise HTTPException(422, "Name can't be empty")
    out = _out_dir(user)
    exists = any(os.path.exists(os.path.join(out, base + s))
                 for s in (".report.md", ".transcript.json", ".meta.json"))
    if not exists:
        raise HTTPException(404, "No such meeting")
    _write_meta(user, base, name=name)
    return {"base": base, "name": name}


@app.delete("/api/meetings/{base}")
def delete_meeting(base: str, user=Depends(auth)):
    """Permanently delete one meeting's audio + report + transcript (this user's
    namespace only). Irreversible. Usage already billed for it is NOT refunded —
    Sarvam was already paid — so the cap ledger is left untouched."""
    base = _safe_base(base)
    paths = [os.path.join(_out_dir(user), base + s)
             for s in (".report.md", ".transcript.json", ".meta.json")]
    paths += [_rec_path(user, base, s)
              for s in (".system.wav", ".mic.wav", ".mix.wav")]
    removed = 0
    for p in paths:
        try:
            os.remove(p); removed += 1
        except OSError:
            pass
    with _jobs_lock:
        _jobs.pop((_uid(user), base), None)
    if not removed:
        raise HTTPException(404, "No such meeting")
    return {"deleted": base, "files_removed": removed}


# ---------------------------------------------------------------------------
# Static front-end. Mounted at the ROOT (not /static) so asset paths are
# relative and work identically here and inside the Tauri app (which serves this
# same dir from its root). Mounted LAST so the /api/* routes above win;
# html=True serves index.html at "/".
# ---------------------------------------------------------------------------
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
