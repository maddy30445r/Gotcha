#!/usr/bin/env python3
"""
VALIDATION SPIKE #1 — Hinglish dev-meeting transcription accuracy
=================================================================
Goal: find out, cheaply, whether Sarvam Saaras v3 transcribes YOUR real
standup / KT audio well enough (Hinglish + dev terms + speaker separation)
to build the whole product on top of. No UI. Just truth.

USAGE
  1. Sign up at https://dashboard.sarvam.ai and copy your API key.
  2. Add your key one of two ways:
       a) cp .env.example .env  then put your key in .env  (recommended), or
       b) export SARVAM_API_KEY="your_key_here"  in your shell.
     (Install deps first: pip install -r requirements.txt)
  3. Record a real meeting (3-10 min is plenty for the test). Any format:
     wav / mp3 / m4a / ogg / opus / flac / webm. Mono or stereo both fine.
  4. python3 transcribe_meeting.py path/to/your_meeting.m4a

WHAT IT DOES
  - Sends the file to the Batch API (needed for >30s audio AND for diarization)
  - mode="codemix"  -> natural Hinglish output (keeps English words in Roman)
  - with_diarization=True -> "who said what" (Speaker 0/1/2...)
  - with_timestamps=True  -> needed later for the cite-to-transcript trust layer
  - Saves the raw JSON output and prints a readable speaker-tagged transcript

WHAT TO LOOK FOR (this is the actual test)
  [ ] Does it get the technical terms? (PR, staging, regression, deploy,
      your module/API names) — or does it mangle them?
  [ ] Does diarization actually separate speakers, or smear them together?
  [ ] Is the Hinglish natural and readable, or broken at every code-switch?
  If yes/yes/yes -> you have a product. If no -> you learned it for ~free.
"""

import os
import sys
import json
import glob


def main():
    if len(sys.argv) < 2:
        sys.exit("Usage: python3 transcribe_meeting.py <audio_file>")

    audio_path = sys.argv[1]
    if not os.path.exists(audio_path):
        sys.exit(f"File not found: {audio_path}")

    # Load SARVAM_API_KEY from a .env file (if present) without overriding
    # any value already set in the real environment.
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass  # python-dotenv not installed — fall back to plain env vars

    api_key = os.environ.get("SARVAM_API_KEY")
    if not api_key:
        sys.exit(
            "No SARVAM_API_KEY found.\n"
            "  Add it to a .env file (copy .env.example to .env), or\n"
            "  export SARVAM_API_KEY=... in your shell."
        )

    from sarvamai import SarvamAI

    client = SarvamAI(api_subscription_key=api_key)

    print(f"→ Creating batch job (saaras:v3, codemix, diarization on)")
    job = client.speech_to_text_job.create_job(
        model="saaras:v3",
        mode="codemix",          # natural Hinglish; swap to "transcribe" to compare
        with_diarization=True,    # who-said-what
        with_timestamps=True,     # word/segment timing for later citations
        language_code="unknown",  # let it auto-detect
        # num_speakers=3,         # optionally hint speaker count if you know it
    )

    print(f"→ Uploading {audio_path}")
    job.upload_files(file_paths=[audio_path])

    print("→ Starting job")
    job.start()

    print("→ Waiting for completion (polling)...")
    job.wait_until_complete(poll_interval=5, timeout=1800)

    if not job.is_successful():
        print("Job did not succeed. Status:")
        print(job.get_status())
        sys.exit(1)

    out_dir = "sarvam_output"
    os.makedirs(out_dir, exist_ok=True)
    job.download_outputs(output_dir=out_dir)
    print(f"→ Raw output saved to ./{out_dir}/")

    # Pretty-print whatever transcript JSON came back
    json_files = glob.glob(os.path.join(out_dir, "*.json"))
    if not json_files:
        print("No JSON output found — inspect the folder manually.")
        return

    for jf in json_files:
        print("\n" + "=" * 70)
        print(f"FILE: {jf}")
        print("=" * 70)
        with open(jf) as f:
            data = json.load(f)

        # The diarized transcript lives under 'diarized_transcript' -> 'entries'
        # (field names can shift between API versions; fall back to raw dump)
        printed = False
        dt = data.get("diarized_transcript")
        if isinstance(dt, dict) and dt.get("entries"):
            for e in dt["entries"]:
                spk = e.get("speaker_id", e.get("speaker", "?"))
                start = e.get("start_time_seconds", e.get("start", ""))
                text = e.get("transcript", e.get("text", ""))
                print(f"[{start:>6}s] Speaker {spk}: {text}")
            printed = True

        if not printed and data.get("transcript"):
            print("(no diarization entries — plain transcript:)\n")
            print(data["transcript"])
            printed = True

        if not printed:
            print("(unrecognized shape — dumping raw JSON keys:)")
            print(list(data.keys()))
            print(json.dumps(data, ensure_ascii=False, indent=2)[:3000])


if __name__ == "__main__":
    main()