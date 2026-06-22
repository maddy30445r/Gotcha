#!/usr/bin/env python3
"""
VALIDATION SPIKE #2 (Gemini free-tier version)
==============================================
Runs the SAME interpretation prompt + SAME transcript against Gemini Flash,
so you can see how a free, cost-efficient model handles the core IP and compare
it to the Opus output.

SETUP (no credit card)
  1. Go to https://aistudio.google.com  -> "Get API key" -> create key
  2. export GEMINI_API_KEY="your_key_here"
  3. pip install google-genai --break-system-packages
  4. python3 test_interpretation_gemini.py

NOTES
  - Uses gemini-2.5-flash (free tier: ~250 req/day, plenty for testing).
  - PRIVACY: this script uses your FAKE scripted transcript, which is fine. Do
    NOT run real company meetings through the Gemini FREE tier — Google may use
    free-tier inputs to improve their models. Real product => paid/no-train tier.
  - Has simple retry on 429 rate-limit errors.
  - To try the weaker/cheaper model, change MODEL to "gemini-2.5-flash-lite".
"""

import os, time, sys

MODEL = "gemini-3.5-flash"

SYSTEM_PROMPT = r"""
You are a meeting interpreter for software teams. You read a transcript of a
Hinglish (Hindi-English code-mixed) developer meeting and explain it to ONE
specific user: Shivam. Your job is NOT to summarize neutrally — it is to tell
Shivam what was decided, what their lead actually meant, and exactly what they
personally now have to do.

## Inputs
- TARGET USER: Shivam
- KNOWN TERMS (real project/client/tool names, used to fix garbled
  transcription): Feather (PMS product), BookingPal / BPAL (integration), AML
  (Assisted Math Learning platform), Jira, MongoDB, Worldpay, Blink Hellas,
  Delhi (data migration), DevOps, staging, 3DS.
- TRANSCRIPT: diarized entries with speaker_id, start time, text. Speaker IDs are
  UNRELIABLE (diarization merges turn-boundaries and flips IDs).

## Before interpreting (silently)
1. Correct obvious ASR errors using KNOWN TERMS (e.g. "wordplay"->Worldpay,
   "Daily Data Migration"->Delhi Data Migration, "EML"->AML, "booking
   PAL"/"BPL"->BookingPal/BPAL, dropped "Jira"). Only when confident; never invent.
2. Re-derive roles from CONTENT not speaker_id. The LEAD assigns work, sets
   priorities/deadlines, grants approval. Shivam reports status and receives
   instructions. If diarization and content disagree, trust content. If a line's
   speaker is genuinely ambiguous, say so.

## Output — exactly these three sections

### 1. Meeting summary & decisions
3–6 plain sentences. What it was about and what was decided/prioritized. Lead with
what matters most to Shivam.

### 2. What the lead really meant
Only for indirect/vague/implied lines. Quote briefly (<15 words) then give the
plain meaning. Soft warnings and implied expectations should be stated directly.
If nothing implicit, write "Nothing implicit — the lead was direct throughout."
Do not manufacture hidden meaning.

### 3. Shivam's action items
A list. ONLY Shivam's own tasks. For each: the task in one line; **priority** if
stated/implied; **why** in a few words; **source** = timestamp + short quote
(<15 words). Flag ambiguous ownership with "(verify — unclear if yours)". Never
assign Shivam someone else's task.

## CITATION RULES — STRICT (this is the trust mechanism; violating it is worse than no citation)
- Each TRANSCRIPT entry has a specific start time (the number before "s") and a
  block of text. When you cite a source, you MUST:
  1. Use a timestamp that appears VERBATIM as the start time of an actual entry in
     the input. NEVER invent, estimate, compute, or interpolate a timestamp. If a
     claim comes from inside a long entry, cite that entry's start time — do not
     guess a finer-grained time that isn't in the input.
  2. Quote text that appears VERBATIM inside that same entry. Do not merge words
     from different entries into one quote. Copy the exact words; do not paraphrase
     inside the quotes.
  3. Keep the quote under 15 words.
- Before finalizing, re-check every timestamp you wrote against the input. If a
  timestamp is not an exact start time of some entry, FIX IT to the correct entry's
  start time. This self-check is mandatory.
- The valid timestamps in this input are exactly: 1.49, 5.03, 28.83, 28.99, 30.33,
  48.01, 48.75, 49.51, 52.55, 61.97, 62.95, 72.75, 73.09, 73.59. You may ONLY cite
  from this list.

## Rules
- Every action item and every "lead meant" claim MUST cite timestamp + short
  quote (<15 words). No uncited claims.
- Do not hallucinate tasks/deadlines/meanings. Under-reporting beats inventing.
- Address Shivam directly ("you need to…"), warm and concise.
""".strip()

TRANSCRIPT_ENTRIES = [
    {"speaker_id":"0","t":1.49,"text":"हाँ शिवम start करो कल क्या किया और आज का plan बताओ"},
    {"speaker_id":"1","t":5.03,"text":"हां तो मैंने कल feather का multi-calendar वाला UI overall continue किया। 2 zero tickets close किए। 1 था calendar sync का S case जहां overlapping bookings गलत render हो रहे थे। और 2nd date range picker का regression fix। आज मैं इन booking PAL integration का sync issue देखूंगा। जहां से BPL के records थोड़े still आ रहे हैं। 1 छोटा blocker है staging को deploy करने के लिए मुझे DevOps approval चाहिए।"},
    {"speaker_id":"1","t":28.83,"text":"हाँ"},
    {"speaker_id":"0","t":28.99,"text":"ओके वो approval का देख"},
    {"speaker_id":"1","t":30.33,"text":"देख लेंगे। 1 बात, पिछली बार जैसा हुआ था ना, deploy करने से पहले थोड़ा ध्यान रखना। तुम समझ रहे हो ना मैं क्या कह रहा हूं। हां हां, समझ गया। और 1 चीज, EML site पर Daily Data Migration का batch कल रात time out कर गया था। Around 4400 learners पे। MongoDB aggregation pipeline optimize करना पड़ेगा।"},
    {"speaker_id":"0","t":48.01,"text":"शायद"},
    {"speaker_id":"1","t":48.75,"text":"कनेक्शन पूल क्या"},
    {"speaker_id":"0","t":49.51,"text":"बढ़ाना होगा तुम lunch के बाद आकर देख लो"},
    {"speaker_id":"1","t":52.55,"text":"ठीक है, मैं देख लेता हूँ। वैसे 3DS authentication testing भी pending है Blink Helas के लिए। wordplay वाला flow वो मैं कल pick करूँ?"},
    {"speaker_id":"0","t":61.97,"text":"हाँ कल कर लेना"},
    {"speaker_id":"1","t":62.95,"text":"अभी कैलेंटर वाला priority है। client ने कहा है demo Friday तक चाहिए। तुम देख लो कि Friday तक ये ready हो जाए। बाकी सब उसी हिसाब से plan करना। कोई और blocker?"},
    {"speaker_id":"0","t":72.75,"text":"नहीं"},
    {"speaker_id":"1","t":73.09,"text":"हाँ"},
    {"speaker_id":"0","t":73.59,"text":"चलो फिर bye bye।"},
]

def main():
    # Load keys from a .env file (if present) without overriding real env vars.
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass  # python-dotenv not installed — fall back to plain env vars

    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        sys.exit(
            "No GEMINI_API_KEY found.\n"
            "  Add it to your .env file, or export GEMINI_API_KEY=... in your shell."
        )

    try:
        from google import genai
        from google.genai import types
    except ImportError:
        sys.exit("Install the SDK first:  pip install google-genai --break-system-packages")

    client = genai.Client(api_key=key)

    transcript_text = "\n".join(
        f'[{e["t"]}s] (diarized speaker {e["speaker_id"]}): {e["text"]}'
        for e in TRANSCRIPT_ENTRIES
    )
    user_msg = ("Here is the diarized transcript. Produce the three sections.\n\n"
                + transcript_text)

    # simple retry on rate-limit / transient errors
    for attempt in range(5):
        try:
            resp = client.models.generate_content(
                model=MODEL,
                contents=user_msg,
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_PROMPT,
                    temperature=0.3,
                    max_output_tokens=4000,
                    # Thinking models count reasoning tokens against the output
                    # budget; disable so the full three-section answer fits.
                    thinking_config=types.ThinkingConfig(thinking_budget=0),
                ),
            )
            print(resp.text)

            # Persist the output so each run is saved (and diffable later).
            out_dir = "gemini_output"
            os.makedirs(out_dir, exist_ok=True)
            md_path = os.path.join(out_dir, "interpretation.md")
            with open(md_path, "w", encoding="utf-8") as f:
                f.write(f"# Gemini interpretation ({MODEL})\n\n")
                f.write(resp.text or "")
                f.write("\n")
            print(f"\n→ Saved interpretation to ./{md_path}", file=sys.stderr)
            return
        except Exception as ex:
            msg = str(ex)
            if "429" in msg or "RESOURCE_EXHAUSTED" in msg:
                wait = min(60, 2 ** attempt)
                print(f"[rate-limited, retrying in {wait}s]", file=sys.stderr)
                time.sleep(wait)
                continue
            sys.exit(f"API error: {ex}")
    sys.exit("Gave up after retries (rate limited).")

if __name__ == "__main__":
    main()