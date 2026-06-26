# Gotcha — Soft Launch Checklist

Prep for putting Gotcha in front of online communities (soft/controlled, no-spend).
Plan: `~/.claude/plans/okay-so-what-can-majestic-frog.md`.

---

## ✅ Done in code (this session)
- **Global spend backstop** — `GOTCHA_GLOBAL_DAILY_CAP_MIN` caps total *paid* Sarvam
  minutes/day across all users. Over the ceiling, meetings **park** (audio kept) instead of
  burning budget. Enforced in `webapp/server.py` upload, worker dequeue, and `/api/process`.
- **Rate limiting** — in-memory throttles on `/api/upload` (per account), `/api/request-access`
  and `/api/auth/email/start` (per IP + per email). Returns 429 when exceeded.
- **Google-first sign-in** — `login.html` now nudges Google as the reliable path and warns that
  alpha email links can be slow/spammy (true until a custom domain exists).
- **Trust pages** — new `privacy.html` + `terms.html` (`/privacy`, `/terms`, linked in the
  landing footer), with an honest data-handling description + a **recording-consent** notice.
- **Accurate privacy copy** — landing + `download.html` no longer overclaim; "encrypted in
  transit + on encrypted disks, no-training AI tier for real meetings."
- **Install friction documented** — `download.html` explains the Gatekeeper right-click→Open
  step and the `xattr -dr com.apple.quarantine` fix for the "damaged/can't open" error.

> ⚠️ Contact email on the privacy/terms pages is currently `shivam.yadav@devslane.com`.
> Swap it for a dedicated address if you'd rather not expose that one.

---

## 🔧 Must-do before sharing the link (operational — only you can do these)

### 1. Turn on Google sign-in (without it, nobody can get in — email is dead without a domain)
- console.cloud.google.com → new project → **OAuth consent screen**
  - User type **External**; app name "Gotcha"; add your support + developer email.
  - Scopes: just `openid`, `email`, `profile` (non-sensitive → no Google verification needed).
  - **Publishing:** choose **"Testing"** to keep a ≤100 manually-added-user gate (a built-in
    soft cap), or **"In production"** for true open self-serve (users see a one-time
    "Google hasn't verified this app" screen — fine for a technical soft launch).
- **Credentials → Create OAuth client ID → Web application**
  - Authorized redirect URI **exactly**:
    `https://gotcha-app.duckdns.org/api/auth/google/callback`
- Put `GOOGLE_CLIENT_ID` + `GOOGLE_CLIENT_SECRET` in `deploy/.env` on the server.

### 2. Flip the LLM to the no-train tier (keeps the privacy promise honest)
In `deploy/.env`:
```
ANTHROPIC_API_KEY=<your real Anthropic key>   # NOT named CLAUDE_KEY — code reads ANTHROPIC_API_KEY
GOTCHA_LLM_PROVIDER=anthropic
GOTCHA_LLM_MODEL=claude-haiku-4-5
```

### 3. Set the budget backstop + confirm session secret
```
GOTCHA_GLOBAL_DAILY_CAP_MIN=600     # ~$8/day max paid; tune to your comfort
GOTCHA_SESSION_SECRET=<stable random>   # else every restart logs everyone out
```
Then redeploy: `cd ~/Gotcha/deploy && docker compose up -d --build`.

### 4. Verify / rebuild + publish the Mac app
- Confirm the published DMG (`maddy30445r/Gotcha` `v0.1.0-alpha`) is the **current** build
  (loopback sign-in + Google OAuth + v3 UI). If unsure, rebuild and re-upload:
  `./gotcha-desktop/build-dmg.sh` → attach `Gotcha.dmg` to the GitHub release.
- On a Mac where it's never run: download → drag → right-click→Open → grant mic + screen →
  sign in with Google → record a 1-min meeting → confirm cited report + audio seek.

### 5. End-to-end test as a stranger
- Second Google account, clean browser → `gotcha-app.duckdns.org` → sign in → 30-min cap.
- Temporarily set `GOTCHA_GLOBAL_DAILY_CAP_MIN=1`, upload → confirm it **parks** (no Sarvam
  spend), then restore the real value.

---

## 📣 Launch kit (the actual posting)

**Assets**
- 20–40s screen capture of the signature moment: a `[timestamp]` chip → audio jumps to that
  second. (GIF for Reddit/HN, short video for elsewhere.)
- One-liner: *"Just say Gotcha in your meetings and offload the hard parts to us — it decodes
  what your lead actually meant and your action items, each pinned to the exact second."*
- Honesty block (paste in every post): *alpha · macOS only · self-signed so first launch needs
  right-click→Open · free during beta.*

**Where (Mac-first dev tool, soft launch — pick 2–3 to start)**
- r/macapps, r/macOS, r/SideProject · Indie Hackers · Lobsters
- A couple of Mac/dev Discords or Slacks you're already in
- **Show HN** ("Show HN: Gotcha – a meeting decoder for Hinglish standups, with cited audio")
- *Hold* Product Hunt + big subreddits for the later splash (after notarization + a domain).

**Lead with the 3 real differentiators, not "AI notetaker":**
interprets (not just summarizes) · two-track you-vs-them attribution · replayable cited proof.
Exclude the wrong audience up front (it's for ICs in fast Hinglish standups, not solo founders).

**Feedback loop:** one channel (a Discord invite or a short form) + ask explicitly: *"what
confused you in the first 5 minutes?"*

---

## 🕒 Deferred (revisit before the big splash)
- Apple notarization ($99/yr) → removes the Gatekeeper wall.
- Custom domain + real email ($12/yr) → unlocks magic-link + brand trust. **Highest-leverage
  thing currently skipped.**
- Windows sidecar · deeper monitoring/Sentry · mobile-responsive polish.
