"""Self-serve auth + account store for Gotcha (Milestone 2).

A SQLite-backed user store (replacing the static users.json), stateless signed
session cookies for the web, and a long-lived per-user API token for the desktop
app + CLI bearer flow.

The user RECORD this module returns matches the legacy users.json record shape
exactly — {user_id, display_name, email?, cap_minutes?, glossary?, hotwords?,
provider?, model?} — so server.py's _cfg_for / _uid / storage namespacing /
metering are completely unchanged. Only the *source* of the record (SQLite) and
the *proof of identity* (bearer api_token OR session cookie) are new.

Stdlib only (sqlite3, hmac) so Phase 1 needs no new dependencies. Email
(magic-link) and Google OAuth land in later phases.
"""
import os
import json
import time
import hmac
import base64
import hashlib
import secrets
import sqlite3
import threading

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
DATA_ROOT = os.environ.get("GOTCHA_DATA_DIR", _ROOT)
DB_PATH = os.path.join(DATA_ROOT, "gotcha.db")

# New accounts get a small free cap so open signup can't drain the managed
# Sarvam/LLM budget. Raise per-user in the DB when you want.
FREE_CAP_MIN = float(os.environ.get("GOTCHA_FREE_CAP_MIN", "30"))

# Session cookie signing key. Set GOTCHA_SESSION_SECRET in prod so sessions
# survive restarts; in dev we fall back to an ephemeral key (fine for one box).
SESSION_SECRET = os.environ.get("GOTCHA_SESSION_SECRET") or secrets.token_hex(32)
SESSION_TTL = 30 * 24 * 3600  # 30 days

# Public base URL (for absolute links in emails / OAuth redirects). Falls back to
# the request's own base_url when unset (dev).
PUBLIC_URL = (os.environ.get("GOTCHA_PUBLIC_URL") or "").rstrip("/")
# Transactional email via Resend. No key (dev) → links print to the console.
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
RESEND_FROM = os.environ.get("RESEND_FROM", "Gotcha <onboarding@resend.dev>")


def send_email(to, subject, html):
    """Send one transactional email via Resend. In dev (no RESEND_API_KEY) it
    prints to the console instead, so the magic link is still usable end-to-end."""
    if not RESEND_API_KEY:
        print(f"\n[email:dev] to={to}\n  subject: {subject}\n  {html}\n")
        return True
    import httpx
    try:
        r = httpx.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
            json={"from": RESEND_FROM, "to": [to], "subject": subject, "html": html},
            timeout=15,
        )
        if r.status_code >= 300:
            print(f"[email] Resend error {r.status_code}: {r.text[:200]}")
            return False
        return True
    except Exception as ex:
        print(f"[email] send failed: {ex}")
        return False

_write_lock = threading.Lock()


# --------------------------------------------------------------------------- DB
def _conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    os.makedirs(DATA_ROOT, exist_ok=True)
    with _conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS users(
            user_id     TEXT PRIMARY KEY,
            email       TEXT UNIQUE,
            display_name TEXT,
            created_at  REAL,
            cap_minutes REAL,
            glossary    TEXT,   -- JSON array
            hotwords    TEXT,   -- JSON array
            provider    TEXT,
            model       TEXT,
            api_token   TEXT UNIQUE
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS magic_links(
            token      TEXT PRIMARY KEY,
            email      TEXT,
            expires_at REAL,
            used       INTEGER DEFAULT 0
        )""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_users_api ON users(api_token)")
        # Migration: last time the desktop app was seen talking to the backend (used to
        # tell the web app whether a Mac app is connected). Idempotent on existing DBs.
        try:
            c.execute("ALTER TABLE users ADD COLUMN desktop_seen_at REAL")
        except sqlite3.OperationalError:
            pass  # column already exists


def _row_to_record(r):
    """Map a users row → the legacy user-record dict server.py expects."""
    if not r:
        return None
    rec = {
        "user_id": r["user_id"],
        "display_name": r["display_name"] or r["user_id"],
        "email": r["email"],
    }
    if "desktop_seen_at" in r.keys():
        rec["desktop_seen_at"] = r["desktop_seen_at"]
    if r["cap_minutes"] is not None:
        rec["cap_minutes"] = r["cap_minutes"]
    for k in ("glossary", "hotwords"):
        if r[k]:
            try:
                rec[k] = json.loads(r[k])
            except Exception:
                pass
    if r["provider"]:
        rec["provider"] = r["provider"]
    if r["model"]:
        rec["model"] = r["model"]
    return rec


# ------------------------------------------------------------------- lookups
def user_by_api_token(token):
    with _conn() as c:
        return _row_to_record(
            c.execute("SELECT * FROM users WHERE api_token=?", (token,)).fetchone())


def user_by_id(user_id):
    with _conn() as c:
        return _row_to_record(
            c.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone())


def user_by_email(email):
    with _conn() as c:
        return _row_to_record(
            c.execute("SELECT * FROM users WHERE email=?", (email.lower(),)).fetchone())


def api_token_for(user_id):
    with _conn() as c:
        r = c.execute("SELECT api_token FROM users WHERE user_id=?", (user_id,)).fetchone()
        return r["api_token"] if r else None


def touch_desktop_seen(user_id, when=None):
    """Record that the desktop app was just seen talking to the backend (lets the web
    app tell whether a Mac app is connected)."""
    with _conn() as c:
        c.execute("UPDATE users SET desktop_seen_at=? WHERE user_id=?",
                  (when if when is not None else time.time(), user_id))


# ------------------------------------------------------------------- create
def _new_api_token():
    return "gk_" + secrets.token_urlsafe(24)


def _new_user_id(email):
    # Stable, filesystem-safe namespace derived from the email.
    return "u_" + hashlib.sha1(email.lower().encode()).hexdigest()[:12]


def find_or_create_user(email, display_name=None):
    """Return (record, created). Open signup: a new email becomes a new account
    with the free cap."""
    email = (email or "").lower().strip()
    existing = user_by_email(email)
    if existing:
        return existing, False
    uid = _new_user_id(email)
    name = (display_name or email.split("@")[0]).strip() or uid
    with _write_lock, _conn() as c:
        c.execute(
            "INSERT INTO users(user_id,email,display_name,created_at,cap_minutes,api_token)"
            " VALUES(?,?,?,?,?,?)",
            (uid, email, name, time.time(), FREE_CAP_MIN, _new_api_token()))
    return user_by_email(email), True


def migrate_users_json(users_file):
    """One-time import of the legacy users.json (token→record) into the DB, so
    existing minted testers keep working — their old token becomes their
    api_token. No-op once the users table has any rows. Returns count imported."""
    if not os.path.exists(users_file):
        return 0
    with _conn() as c:
        if c.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
            return 0
    try:
        with open(users_file, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return 0
    count = 0
    with _write_lock, _conn() as c:
        for token, rec in data.items():
            uid = rec.get("user_id")
            if not uid:
                continue
            c.execute(
                "INSERT OR IGNORE INTO users(user_id,email,display_name,created_at,"
                "cap_minutes,glossary,hotwords,provider,model,api_token)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (uid, rec.get("email"), rec.get("display_name"), time.time(),
                 rec.get("cap_minutes"),
                 json.dumps(rec["glossary"]) if rec.get("glossary") else None,
                 json.dumps(rec["hotwords"]) if rec.get("hotwords") else None,
                 rec.get("provider"), rec.get("model"), token))
            count += 1
    return count


# ----------------------------------------------------------- magic links (P2)
def create_magic_link(email, ttl=900):
    """Mint a single-use magic-link token (default 15 min). Emailing it is the
    caller's job (Phase 2)."""
    token = secrets.token_urlsafe(32)
    with _write_lock, _conn() as c:
        c.execute("INSERT INTO magic_links(token,email,expires_at,used) VALUES(?,?,?,0)",
                  (token, (email or "").lower().strip(), time.time() + ttl))
    return token


def consume_magic_link(token):
    """Validate + burn a magic-link token. Returns the email or None."""
    with _write_lock, _conn() as c:
        r = c.execute("SELECT * FROM magic_links WHERE token=?", (token,)).fetchone()
        if not r or r["used"] or r["expires_at"] < time.time():
            return None
        c.execute("UPDATE magic_links SET used=1 WHERE token=?", (token,))
        return r["email"]


# ------------------------------------------------------------- session cookie
def _b64e(b):
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _b64d(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


SESSION_COOKIE = "gotcha_session"


def make_session(user_id, ttl=SESSION_TTL):
    """A stateless signed cookie value: base64(payload).hmac — no session table."""
    raw = _b64e(json.dumps({"uid": user_id, "exp": time.time() + ttl}).encode())
    sig = _b64e(hmac.new(SESSION_SECRET.encode(), raw.encode(), hashlib.sha256).digest())
    return raw + "." + sig


def read_session(cookie):
    """Return the user_id from a valid, unexpired session cookie, else None."""
    if not cookie or "." not in cookie:
        return None
    try:
        raw, sig = cookie.split(".", 1)
        expect = _b64e(hmac.new(SESSION_SECRET.encode(), raw.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expect):
            return None
        payload = json.loads(_b64d(raw))
        if payload.get("exp", 0) < time.time():
            return None
        return payload.get("uid")
    except Exception:
        return None


def sign_payload(data, ttl=600):
    """Generic short-lived signed token (same HMAC scheme as the session cookie) for
    carrying small state across a redirect round-trip — e.g. the desktop loopback target."""
    body = dict(data)
    body["exp"] = time.time() + ttl
    raw = _b64e(json.dumps(body).encode())
    sig = _b64e(hmac.new(SESSION_SECRET.encode(), raw.encode(), hashlib.sha256).digest())
    return raw + "." + sig


def read_payload(value):
    """Return the dict from a valid, unexpired signed payload, else None."""
    if not value or "." not in value:
        return None
    try:
        raw, sig = value.split(".", 1)
        expect = _b64e(hmac.new(SESSION_SECRET.encode(), raw.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expect):
            return None
        data = json.loads(_b64d(raw))
        if data.get("exp", 0) < time.time():
            return None
        return data
    except Exception:
        return None


# --------------------------------------------------------------- Google OAuth
# Server-side ("web application") OAuth: redirect to consent → exchange the code
# for the user's email/name over httpx. No client-side JS SDK, so no JS origin.
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"


def google_enabled():
    return bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)


def make_oauth_state(client, ttl=600):
    """A short-lived signed state (same HMAC scheme as the session cookie) that
    carries the originating client (web|desktop) through the round-trip and is the
    CSRF guard — the callback rejects any state we didn't sign."""
    raw = _b64e(json.dumps(
        {"client": client, "exp": time.time() + ttl, "n": secrets.token_urlsafe(8)}).encode())
    sig = _b64e(hmac.new(SESSION_SECRET.encode(), raw.encode(), hashlib.sha256).digest())
    return raw + "." + sig


def read_oauth_state(state):
    """Return the client (web|desktop) from a valid state, else None."""
    if not state or "." not in state:
        return None
    try:
        raw, sig = state.split(".", 1)
        expect = _b64e(hmac.new(SESSION_SECRET.encode(), raw.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expect):
            return None
        payload = json.loads(_b64d(raw))
        if payload.get("exp", 0) < time.time():
            return None
        return payload.get("client", "web")
    except Exception:
        return None


def google_auth_url(redirect_uri, state):
    from urllib.parse import urlencode
    q = urlencode({
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "access_type": "online",
        "prompt": "select_account",
    })
    return f"{GOOGLE_AUTH_URL}?{q}"


def google_exchange(code, redirect_uri):
    """Exchange an auth code → (email, name). Returns (None, None) on any failure
    (so the caller bounces back to /login with an error rather than 500-ing)."""
    import httpx
    try:
        tok = httpx.post(GOOGLE_TOKEN_URL, data={
            "code": code,
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        }, timeout=15)
        if tok.status_code >= 300:
            print(f"[oauth] token error {tok.status_code}: {tok.text[:200]}")
            return None, None
        access = tok.json().get("access_token")
        if not access:
            return None, None
        ui = httpx.get(GOOGLE_USERINFO_URL,
                       headers={"Authorization": f"Bearer {access}"}, timeout=15)
        if ui.status_code >= 300:
            print(f"[oauth] userinfo error {ui.status_code}: {ui.text[:200]}")
            return None, None
        d = ui.json()
        email = (d.get("email") or "").lower().strip()
        if not email or d.get("email_verified") is False:
            return None, None
        return email, d.get("name")
    except Exception as ex:
        print(f"[oauth] exchange failed: {ex}")
        return None, None
