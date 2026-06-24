#!/usr/bin/env python3
"""
MEETING DECODER — thin end-to-end pipeline (v0.1)
=================================================
audio file  ->  Sarvam (Hinglish + diarization + timestamps)
            ->  strict interpretation (summary / lead-decode / your tasks, cited)
            ->  printed report

This is the "tool for yourself" build. Run it after your own standups for a
couple weeks and see if you keep reaching for it. Not a product yet — no UI, no
auth, no multi-user. Just the proven core loop, wired together.

SETUP
  pip install sarvamai google-genai --break-system-packages
  export SARVAM_API_KEY="..."      # from dashboard.sarvam.ai
  export GEMINI_API_KEY="..."      # from aistudio.google.com  (or use a paid/no-train tier for REAL meetings)

USAGE
  python3 pipeline.py path/to/meeting.m4a                 # single file
  python3 pipeline.py meeting.system.wav meeting.mic.wav  # two-track (you vs them)
  python3 pipeline.py pipeline_output/meeting.transcript.json  # re-interpret, free

  # The two-track jobs run in parallel and the transcript is saved before
  # interpretation, so if Gemini fails you re-run the third form for free (no
  # paying Sarvam to re-transcribe).
  #
  # Per-run settings (your name, team glossary, LLM) live in UserConfig below.
  # The CLI uses DEFAULT_CONFIG (override via GOTCHA_YOUR_NAME / GOTCHA_LLM_*);
  # the multi-user backend builds one UserConfig per user and passes cfg=... .
  # The glossary fixes garbled proper nouns (Worldpay, Delhi, AML, BookingPal…).

PRIVACY WARNING
  Gemini's FREE tier may train on your inputs. Do NOT run real company meetings
  through it. For real meetings use a paid/no-train tier (Gemini paid, Vertex,
  or Anthropic API). This file defaults to Gemini for cheap testing; swap the
  interpret() backend when you go real.
"""

import os, sys, glob, json, time, tempfile, re, difflib
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor

# Load keys from a .env file (if present) without overriding real env vars.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv not installed — fall back to plain env vars

# ----------------------------------------------------------------------------
# CONFIG — per-user, no longer hard-coded
# ----------------------------------------------------------------------------
# Single-user defaults. In the multi-user backend each request builds its own
# UserConfig (the user's name + team glossary + chosen LLM) and threads it
# through; everything below just falls back to these when no cfg is passed, so
# the CLI and the existing webapp keep working unchanged.
DEFAULT_GLOSSARY = [
    "Feather (PMS product)", "BookingPal / BPAL (integration)",
    "AML (Assisted Math Learning platform)", "Jira", "MongoDB", "Worldpay",
    "Blink Hellas", "Delhi (data migration)", "DevOps", "staging", "3DS",
]
# Terms passed to Sarvam as hotwords to reduce proper-noun errors at the source.
DEFAULT_HOTWORDS = ["Feather", "BookingPal", "BPAL", "AML", "Jira", "MongoDB",
                    "Worldpay", "Blink Hellas", "Delhi", "DevOps", "staging", "3DS"]


@dataclass
class UserConfig:
    """Everything that used to be a hard-coded module constant. One per user.

    The LLM is provider + model so switching to a paid/no-train tier (the
    eventual privacy fix) is a config change — set GOTCHA_LLM_PROVIDER /
    GOTCHA_LLM_MODEL (or pass a UserConfig) — not a code edit. See interpret()."""
    your_name: str = "Madhur"
    glossary: list = field(default_factory=lambda: list(DEFAULT_GLOSSARY))
    hotwords: list = field(default_factory=lambda: list(DEFAULT_HOTWORDS))
    # provider: "groq" (free, robust — testing default) | "anthropic" (no-train,
    # for real meetings) | "gemini". Real meetings MUST use a no-train tier.
    provider: str = "groq"
    model: str = "llama-3.3-70b-versatile"


def default_config():
    """The process-wide default config, with optional .env overrides so the LLM
    can be swapped without touching code (e.g. GOTCHA_LLM_MODEL=...)."""
    return UserConfig(
        your_name=os.environ.get("GOTCHA_YOUR_NAME", "Madhur"),
        provider=os.environ.get("GOTCHA_LLM_PROVIDER", "groq"),
        model=os.environ.get("GOTCHA_LLM_MODEL", "llama-3.3-70b-versatile"),
    )


DEFAULT_CONFIG = default_config()

# Back-compat module attributes — some callers (e.g. webapp/server.py) still read
# pipeline.YOUR_NAME. Kept as a thin alias onto the default config.
YOUR_NAME = DEFAULT_CONFIG.your_name
GLOSSARY = DEFAULT_CONFIG.glossary
HOTWORDS = DEFAULT_CONFIG.hotwords
GEMINI_MODEL = DEFAULT_CONFIG.model


# ----------------------------------------------------------------------------
# STEP 1 — transcribe via Sarvam (batch API: handles >30s + diarization)
# ----------------------------------------------------------------------------
def transcribe(audio_path, *, diarize=True, force_speaker=None, label="audio",
               hotwords=None):
    """Transcribe one file. With force_speaker set, skip diarization and stamp
    every entry with that speaker label (used for the mic track, which is a
    single known speaker). hotwords biases the recognizer toward the team's
    proper nouns (defaults to the process-wide config)."""
    if hotwords is None:
        hotwords = DEFAULT_CONFIG.hotwords
    from sarvamai import SarvamAI
    key = os.environ.get("SARVAM_API_KEY")
    if not key:
        raise RuntimeError("Set SARVAM_API_KEY")
    client = SarvamAI(api_subscription_key=key)

    print(f"→ [1/3] Transcribing {label} via Sarvam (Hinglish"
          + (" + diarization" if diarize else "") + ")...", file=sys.stderr)
    job_kwargs = dict(
        model="saaras:v3", mode="codemix",
        with_diarization=diarize, with_timestamps=True,
        language_code="unknown",
    )
    # Bias the recognizer toward the team's proper nouns. The param has moved
    # across Sarvam SDK versions (newer: `prompt`, a comma-joined string; older:
    # a `hotwords` list) and the installed 0.1.28 batch job accepts NEITHER, so
    # try each and fall back rather than failing the run. Not lost when it falls
    # back: the glossary still corrects proper nouns downstream at interpret().
    create = client.speech_to_text_job.create_job
    bias_attempts = ([{"prompt": ", ".join(hotwords)}, {"hotwords": hotwords}]
                     if hotwords else [])
    job = None
    for bias in bias_attempts:
        try:
            job = create(**bias, **job_kwargs)
            break
        except TypeError:
            continue
    if job is None:
        if hotwords:
            print("  (SDK accepts no prompt/hotwords biasing — continuing without; "
                  "glossary still fixes proper nouns at interpret)", file=sys.stderr)
        job = create(**job_kwargs)
    job.upload_files(file_paths=[audio_path])
    job.start()
    job.wait_until_complete(poll_interval=5, timeout=1800)
    if not job.is_successful():
        print(job.get_status(), file=sys.stderr)
        raise RuntimeError(f"Sarvam job failed for {label}")

    out_dir = tempfile.mkdtemp(prefix="sarvam_")
    job.download_outputs(output_dir=out_dir)
    jf = glob.glob(os.path.join(out_dir, "*.json"))
    if not jf:
        raise RuntimeError(f"No transcript JSON returned for {label}")
    with open(jf[0]) as f:
        data = json.load(f)

    # normalize to a list of {speaker_id, t, text}
    entries = []
    dt = data.get("diarized_transcript")
    if isinstance(dt, dict) and dt.get("entries"):
        for e in dt["entries"]:
            entries.append({
                "speaker_id": force_speaker or str(e.get("speaker_id", "?")),
                "t": e.get("start_time_seconds", 0.0),
                "text": e.get("transcript", e.get("text", "")),
            })
    else:
        # fallback: single undiarized blob
        entries.append({"speaker_id": force_speaker or "?", "t": 0.0,
                        "text": data.get("transcript", "")})
    return entries


# ----------------------------------------------------------------------------
# STEP 2 — build the strict interpretation prompt (citation allowlist generated)
# ----------------------------------------------------------------------------
def build_prompt(entries, two_track=False, *, cfg=None):
    cfg = cfg or DEFAULT_CONFIG
    YOUR_NAME = cfg.your_name
    valid_ts = ", ".join(str(e["t"]) for e in entries)
    glossary = "; ".join(cfg.glossary)
    if two_track:
        speaker_block = f"""- TRANSCRIPT: entries with speaker_id, start time, text. Speaker labels are
  RELIABLE for who-vs-{YOUR_NAME}: entries labeled "{YOUR_NAME}" were captured
  from {YOUR_NAME}'s own microphone (definitely {YOUR_NAME}); entries labeled
  "Other"/"Other-N" were captured from the call audio (definitely NOT {YOUR_NAME},
  i.e. the lead/teammates). Trust this split completely. The only thing to infer
  from content is distinguishing multiple "Other" speakers from each other."""
        roles_line = f"""2. Roles: "{YOUR_NAME}" is {YOUR_NAME} (reports status, receives instructions).
   The "Other" speakers are the lead/teammates (assign work, set priorities,
   grant approval). Use content to tell apart multiple "Other" speakers."""
    else:
        speaker_block = """- TRANSCRIPT: diarized entries with speaker_id, start time, text. Speaker IDs are
  UNRELIABLE (diarization merges turn-boundaries and flips IDs)."""
        roles_line = f"""2. Re-derive roles from CONTENT not speaker_id. The LEAD assigns work, sets
   priorities/deadlines, grants approval. {YOUR_NAME} reports status and receives
   instructions. If diarization and content disagree, trust content. If a line's
   speaker is genuinely ambiguous, say so."""
    return f"""
You are a meeting interpreter for software teams. You read a transcript of a
Hinglish (Hindi-English code-mixed) developer meeting and explain it to ONE
specific user: {YOUR_NAME}. Your job is NOT to summarize neutrally — tell
{YOUR_NAME} what was decided, what their lead actually meant, and exactly what
they personally now have to do.

## Inputs
- TARGET USER: {YOUR_NAME}
- KNOWN TERMS (fix garbled transcription using these): {glossary}
{speaker_block}

## Before interpreting (silently)
1. Correct obvious ASR errors using KNOWN TERMS. Only when confident; never invent.
{roles_line}

## Output — exactly these four sections
### 1. Meeting summary & decisions
3–6 plain sentences. Lead with what matters most to {YOUR_NAME}.

### 2. What the lead really meant
Only for indirect/vague/implied lines. Quote briefly (<15 words) then give the
plain meaning. List EVERY indirect/implied line as its own item — do not omit or
merge them. If nothing implicit, write "Nothing implicit — the lead was direct
throughout." Do not manufacture hidden meaning.

### 3. {YOUR_NAME}'s action items
A list. ONLY {YOUR_NAME}'s own tasks. Never assign {YOUR_NAME} someone else's
task. Flag ambiguous ownership with "(verify — unclear if yours)". Keep distinct
tasks as separate action items — do NOT merge a setup/config step and a creation
step into one unless they are genuinely the same action. Completeness matters:
prefer more specific items over fewer broad ones. For each item, use these
sub-bullets:
- the task in one line (the top bullet)
- **Priority:** if stated/implied
- **Why:** in a few words
- **How:** the concrete steps or exact parameters to use, pulled VERBATIM from the
  transcript — a short list or `key: value` details (e.g. the exact IDs/flags/
  values/commands the lead specified). OMIT this **How:** sub-bullet entirely if
  the transcript states no specifics. Never invent steps; under-reporting beats
  inventing.
- **Source:** timestamp + short quote

### 4. Open questions & things to verify
Anything left unresolved, ambiguous in ownership, or that {YOUR_NAME} should
confirm with the lead before acting. One line each + a [timestamp] + short quote.
If there is nothing open, write exactly: "Nothing open — everything was clear."
Do not manufacture doubts.

## CITATION RULES — STRICT (this is the trust mechanism)
- Use a timestamp that appears VERBATIM in this allowlist: {valid_ts}. NEVER
  invent, estimate, compute, or interpolate a timestamp.
- ALWAYS write a timestamp as [N.Ns] with a trailing "s" — e.g. [90.71s], not
  [90.71]. This exact format is required for the clickable audio link to work.
- Quote text VERBATIM from inside that same entry. Do not merge words across
  entries. Keep quotes under 15 words.
- Before finalizing, re-check every timestamp against the allowlist and fix any
  that aren't on it. This self-check is mandatory.

## Rules
- Every action item, every "lead meant" claim, and every open question MUST cite
  timestamp + short quote. **How:** steps must come straight from the cited entry.
- Do not hallucinate tasks/deadlines/meanings/steps. Under-reporting beats inventing.
- Address {YOUR_NAME} directly, warm and concise.
""".strip()


# ----------------------------------------------------------------------------
# STEP 3 — interpret (Gemini backend; swap for a no-train tier on real meetings)
# ----------------------------------------------------------------------------
def interpret(entries, two_track=False, *, cfg=None):
    """Interpret the transcript into the cited three-section report.

    Dispatches on cfg.provider so swapping the free Gemini tier for a paid/
    no-train tier (the privacy fix for real meetings) is a config change, not a
    rewrite. Add a backend below + set cfg.provider/model (or GOTCHA_LLM_*)."""
    cfg = cfg or DEFAULT_CONFIG
    print("→ [3/3] Interpreting (%s/%s)..." % (cfg.provider, cfg.model), file=sys.stderr)
    system_prompt = build_prompt(entries, two_track=two_track, cfg=cfg)
    speaker_word = "speaker" if two_track else "diarized speaker"
    transcript_text = "\n".join(
        f'[{e["t"]}s] ({speaker_word} {e["speaker_id"]}): {e["text"]}'
        for e in entries
    )
    user_msg = ("Here is the diarized transcript. Produce the three sections.\n\n"
                + transcript_text)

    if cfg.provider == "groq":
        return _interpret_groq(system_prompt, user_msg, cfg.model)
    if cfg.provider == "anthropic":
        return _interpret_anthropic(system_prompt, user_msg, cfg.model)
    if cfg.provider == "gemini":
        return _interpret_gemini(system_prompt, user_msg, cfg.model)
    sys.exit(f"Unknown LLM provider: {cfg.provider!r}")


def _is_permanent_llm_error(msg):
    """A 429 that retrying will NEVER clear — the account is out of money/quota,
    not momentarily rate-limited. Backing off just hangs the job ~60s before it
    errors anyway, so fail fast with a clear message instead."""
    low = msg.lower()
    return ("depleted" in low or "billing" in low
            or "quota" in low or "insufficient" in low
            or "exceeded your current quota" in low)


def _is_transient_llm_error(msg):
    if _is_permanent_llm_error(msg):
        return False
    return ("429" in msg or "RESOURCE_EXHAUSTED" in msg
            or "503" in msg or "UNAVAILABLE" in msg
            or "500" in msg or "INTERNAL" in msg)


def _interpret_gemini(system_prompt, user_msg, model):
    from google import genai
    from google.genai import types
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        sys.exit("Set GEMINI_API_KEY")
    client = genai.Client(api_key=key)

    for attempt in range(5):
        try:
            resp = client.models.generate_content(
                model=model, contents=user_msg,
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    temperature=0.3, max_output_tokens=4000,
                    # Thinking models count reasoning tokens against the output
                    # budget; disable so the full three-section answer fits.
                    thinking_config=types.ThinkingConfig(thinking_budget=0)),
            )
            return resp.text
        except Exception as ex:
            msg = str(ex)
            if _is_permanent_llm_error(msg):
                # Out of credits/quota — retrying can't help. Fail fast with a
                # message the UI can show as-is.
                sys.exit("LLM unavailable: account out of credits/quota. "
                         "Top up or swap GEMINI_API_KEY, then re-interpret "
                         "(the transcript is saved, so it's free).")
            if _is_transient_llm_error(msg):
                w = min(60, 2 ** attempt)
                print(f"[transient error, retry in {w}s: {msg[:80]}]", file=sys.stderr)
                time.sleep(w); continue
            sys.exit(f"Interpretation error: {ex}")
    sys.exit("Gave up after retries")


def _retry_llm(call, swap_hint):
    """Shared 5-attempt backoff around an LLM call() -> str. Reuses the
    provider-agnostic error helpers; swap_hint names the key to change on a
    permanent (out-of-quota) failure so the UI message is actionable."""
    for attempt in range(5):
        try:
            return call()
        except Exception as ex:
            msg = str(ex)
            if _is_permanent_llm_error(msg):
                sys.exit("LLM unavailable: account out of credits/quota. "
                         f"Top up or swap {swap_hint}, then re-interpret "
                         "(the transcript is saved, so it's free).")
            if _is_transient_llm_error(msg):
                w = min(60, 2 ** attempt)
                print(f"[transient error, retry in {w}s: {msg[:80]}]", file=sys.stderr)
                time.sleep(w); continue
            sys.exit(f"Interpretation error: {ex}")
    sys.exit("Gave up after retries")


def _interpret_groq(system_prompt, user_msg, model):
    """Groq free tier (Llama-class models). OpenAI-chat shaped. May train on
    inputs — testing only; real meetings must use a no-train provider."""
    from groq import Groq
    key = os.environ.get("GROQ_API_KEY")
    if not key:
        sys.exit("Set GROQ_API_KEY")
    client = Groq(api_key=key)

    def call():
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": system_prompt},
                      {"role": "user", "content": user_msg}],
            temperature=0.3, max_tokens=4000)
        return resp.choices[0].message.content
    return _retry_llm(call, "GROQ_API_KEY")


def _interpret_anthropic(system_prompt, user_msg, model):
    """Anthropic Claude (no-train tier) — the privacy-safe option for real
    meetings. e.g. model=claude-haiku-4-5."""
    import anthropic
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("Set ANTHROPIC_API_KEY")
    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env

    def call():
        resp = client.messages.create(
            model=model, max_tokens=4000, temperature=0.3,
            system=system_prompt,
            messages=[{"role": "user", "content": user_msg}])
        return next((b.text for b in resp.content if b.type == "text"), "")
    return _retry_llm(call, "ANTHROPIC_API_KEY")


def _norm_text(s):
    """Normalize for fuzzy matching: lowercase, strip punctuation/extra spaces."""
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", s.lower())).strip()


def drop_bleed(mine, others, window=1.5, sim=0.72, min_chars=8):
    """Remove mic entries that duplicate a system entry at ~the same time — these
    are the call audio bleeding into the mic (speaker mode, no headphones). The
    user's voice never appears on the system track, so a strong text+time match is
    provably bleed, not the user. Conservative: short backchannels (< min_chars)
    are left alone, and only high-similarity matches are dropped, so we never drop
    the user's own speech. Returns (kept_entries, dropped_count)."""
    kept = []
    dropped = 0
    for e in mine:
        txt = _norm_text(e["text"])
        if len(txt) >= min_chars and any(
            abs(o["t"] - e["t"]) <= window
            and difflib.SequenceMatcher(None, txt, _norm_text(o["text"])).ratio() >= sim
            for o in others
        ):
            dropped += 1
        else:
            kept.append(e)
    return kept, dropped


def _transcribe_system_track(system_path, *, hotwords=None):
    """Transcribe the call (system) track with diarization and relabel its speakers
    as Other / Other-2 / … (everyone who isn't the user; we only need to tell them
    apart from each other, not from the user — that split comes from the separate
    mic track)."""
    others = transcribe(system_path, diarize=True, label="call audio (others)",
                        hotwords=hotwords)
    ids = {}
    for e in others:
        sid = e["speaker_id"]
        if sid not in ids:
            ids[sid] = "Other" if not ids else f"Other-{len(ids) + 1}"
        e["speaker_id"] = ids[sid]
    return others


def transcribe_two_track(system_path, mic_path, *, cfg=None):
    """Two-track mode: transcribe the call (system) with diarization and the mic as
    a single known speaker, then merge into one timeline with reliable labels.

    The two Sarvam jobs run CONCURRENTLY (each spends most of its time polling a
    server-side batch job, so overlapping the waits ~halves wall-clock for the same
    ~2x cost). If one track fails we keep the other — the transcription is the
    paid, expensive part, so a partial result still beats discarding everything."""
    cfg = cfg or DEFAULT_CONFIG
    print("→ Two-track mode: mic = %s, others = diarized call audio "
          "(2 Sarvam jobs in parallel, ~2x cost)." % cfg.your_name, file=sys.stderr)

    # The mic track is mostly silence, so transcribe_mic_vad VAD-trims it first to
    # cut Sarvam cost, then remaps the trimmed transcript back to the real timeline.
    with ThreadPoolExecutor(max_workers=2) as ex:
        f_sys = ex.submit(_transcribe_system_track, system_path, hotwords=cfg.hotwords)
        f_mic = ex.submit(transcribe_mic_vad, mic_path, cfg=cfg)
        others, e_sys = _settle(f_sys)
        mine, e_mic = _settle(f_mic)

    if e_sys and e_mic:
        raise RuntimeError(f"both Sarvam jobs failed — call: {e_sys}; mic: {e_mic}")
    if e_sys:
        print("→ WARNING: call-audio track failed (%s); proceeding with your mic only."
               % e_sys, file=sys.stderr)
        others = []
    if e_mic:
        print("→ WARNING: your mic track failed (%s); proceeding with call audio only."
               % e_mic, file=sys.stderr)
        mine = []

    # Safety net for any residual speaker bleed. The recorder now echo-cancels the
    # mic at capture (native Voice Processing), so this rarely fires — it only mops
    # up leftovers. Drops mic lines that duplicate a call line at the same time.
    if others and mine:
        mine, dropped = drop_bleed(mine, others)
        if dropped:
            print("→ Dropped %d bleed line(s) from your mic track (speaker echo)." % dropped,
                  file=sys.stderr)

    merged = sorted(others + mine, key=lambda e: e["t"])
    return merged


def _settle(future):
    """Resolve a worker future to (result, None) on success or ([], exception) on
    failure — so one track's error doesn't tear down the other."""
    try:
        return future.result(), None
    except Exception as ex:  # noqa: BLE001 — surfaced to the user as a warning
        return [], ex


def transcribe_mic_vad(mic_path, *, cfg=None):
    """Transcribe the mic track, trimming silence locally first (if webrtcvad is
    available) and remapping timestamps back to the original timeline. Falls back
    to transcribing the full track when VAD isn't usable."""
    cfg = cfg or DEFAULT_CONFIG
    import vad

    trimmed, seg_map = vad.trim_silence(mic_path)
    if not trimmed:
        return transcribe(mic_path, diarize=True, force_speaker=cfg.your_name,
                          label="your mic (full track)", hotwords=cfg.hotwords)

    try:
        import wave
        with wave.open(mic_path, "rb") as w:
            orig_secs = w.getnframes() / float(w.getframerate())
        print("→ VAD-trimmed mic: %s — sending only your speech to Sarvam."
              % vad.summarize(seg_map, orig_secs), file=sys.stderr)

        entries = transcribe(trimmed, diarize=True, force_speaker=cfg.your_name,
                             label="your mic (VAD-trimmed)", hotwords=cfg.hotwords)
        for e in entries:
            e["t"] = round(vad.remap(e["t"], seg_map), 2)
        return entries
    finally:
        try:
            os.remove(trimmed)
        except OSError:
            pass


def _load_saved_transcript(path, *, cfg=None):
    """Load a previously saved transcript.json and infer whether it came from
    two-track capture (reliable labels) so the prompt picks the right speaker
    instructions. Lets us re-interpret for free after a Gemini outage — no paying
    Sarvam to re-transcribe."""
    cfg = cfg or DEFAULT_CONFIG
    with open(path, encoding="utf-8") as f:
        entries = json.load(f)
    if not isinstance(entries, list) or not entries:
        sys.exit(f"Not a valid transcript JSON: {path}")
    two_track = any(
        e.get("speaker_id") == cfg.your_name or str(e.get("speaker_id", "")).startswith("Other")
        for e in entries
    )
    return entries, two_track


def main():
    if len(sys.argv) < 2:
        sys.exit("Usage: python3 pipeline.py <audio_file>\n"
                 "   or: python3 pipeline.py <system.wav> <mic.wav>   (two-track)\n"
                 "   or: python3 pipeline.py <saved.transcript.json>  (re-interpret, no re-transcribe)")

    out_dir = "pipeline_output"
    os.makedirs(out_dir, exist_ok=True)

    # Re-interpret mode: feed back a saved transcript.json (skips Sarvam entirely).
    if len(sys.argv) == 2 and sys.argv[1].endswith(".json"):
        path = sys.argv[1]
        if not os.path.exists(path):
            sys.exit(f"File not found: {path}")
        entries, two_track = _load_saved_transcript(path)
        base = os.path.splitext(os.path.basename(path))[0]
        if base.endswith(".transcript"):
            base = base[: -len(".transcript")]
        print("→ Re-interpreting saved transcript (%d segments, %s) — no re-transcription."
              % (len(entries), "two-track" if two_track else "single-track"), file=sys.stderr)
        _interpret_and_save(entries, two_track, out_dir, base)
        return

    two_track = len(sys.argv) >= 3
    inputs = sys.argv[1:3] if two_track else sys.argv[1:2]
    for p in inputs:
        if not os.path.exists(p):
            sys.exit(f"File not found: {p}")

    if two_track:
        entries = transcribe_two_track(inputs[0], inputs[1])
    else:
        entries = transcribe(inputs[0])
    if not entries:
        sys.exit("Transcription produced no segments — nothing to interpret.")
    print("→ [2/3] Transcript ready, %d segments." % len(entries), file=sys.stderr)

    # Strip .system/.mic so both tracks map to one report basename.
    base = os.path.splitext(os.path.basename(inputs[0]))[0]
    for suffix in (".system", ".mic"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]

    # Persist the transcript NOW — the Sarvam transcription is the expensive,
    # already-paid-for part. If interpretation then fails (e.g. Gemini outage),
    # we keep the transcript and can re-interpret for free:
    #   python3 pipeline.py pipeline_output/{base}.transcript.json
    transcript_path = os.path.join(out_dir, f"{base}.transcript.json")
    with open(transcript_path, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)
    print("→ Saved transcript to %s" % transcript_path, file=sys.stderr)

    _interpret_and_save(entries, two_track, out_dir, base)


def _interpret_and_save(entries, two_track, out_dir, base, *, cfg=None):
    report = interpret(entries, two_track=two_track, cfg=cfg)
    print("\n" + "=" * 70)
    print(report)
    with open(os.path.join(out_dir, f"{base}.report.md"), "w", encoding="utf-8") as f:
        f.write(report or "")
        f.write("\n")
    print(f"\n→ Saved report to ./{out_dir}/{base}.report.md", file=sys.stderr)


if __name__ == "__main__":
    main()