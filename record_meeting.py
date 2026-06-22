#!/usr/bin/env python3
"""
MEETING RECORDER (ScreenCaptureKit) — two-track capture for the pipeline
========================================================================
Captures your meeting via macOS ScreenCaptureKit and saves TWO mono WAVs into
./recordings/ :
    {stamp}_{name}.system.wav   — the call / everyone else (system audio)
    {stamp}_{name}.mic.wav      — you (microphone)

Two separate tracks give the pipeline perfect "you vs them" speaker separation
(no fragile voice-based diarization). Then:

    python3 record_meeting.py                  # record, press ENTER to stop
    python3 record_meeting.py --name standup   # custom filename suffix
    python3 record_meeting.py --list-mics       # list microphones
    python3 record_meeting.py --mic <uid>       # pick a specific mic

NO INSTALL, NO AUDIO SETUP
    Unlike the old BlackHole approach, ScreenCaptureKit is built into macOS.
    First run asks for two permissions (one-time):
      • Microphone        — click Allow.
      • Screen Recording  — enable your terminal app in System Settings →
        Privacy & Security → Screen Recording, then FULLY QUIT and reopen the
        terminal (the grant only takes effect after a relaunch).
    "Screen Recording" is just how macOS gates system-audio capture; no video
    is recorded.

PRIVACY: ./recordings/ is gitignored. Don't run real company audio through
Gemini's free tier (see pipeline.py).
"""

import os
import sys
import signal
import argparse
import datetime
import threading
import subprocess


HERE = os.path.dirname(os.path.abspath(__file__))
RECORDINGS_DIR = os.path.join(HERE, "recordings")
PKG_DIR = os.path.join(HERE, "mac_recorder")
BINARY = os.path.join(PKG_DIR, ".build", "release", "mac-recorder")


def ensure_built():
    """Build the Swift helper if the binary is missing or any source is newer."""
    sources = [os.path.join(PKG_DIR, "Package.swift")]
    for root, _, files in os.walk(os.path.join(PKG_DIR, "Sources")):
        sources += [os.path.join(root, f) for f in files]

    fresh = os.path.exists(BINARY) and all(
        os.path.getmtime(BINARY) >= os.path.getmtime(s) for s in sources
    )
    if fresh:
        return

    print("→ Building native recorder (first run, ~15s)…", file=sys.stderr)
    try:
        subprocess.run(
            ["swift", "build", "--package-path", PKG_DIR, "-c", "release"],
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as ex:
        sys.exit(f"Failed to build native recorder: {ex}\n"
                 "Is Xcode / the Swift toolchain installed?")


def list_mics():
    subprocess.run([BINARY, "--list-mics"], check=False)


def record(out_system, out_mic, mic_uid):
    cmd = [BINARY, "--out-system", out_system, "--out-mic", out_mic]
    if mic_uid:
        cmd += ["--mic", mic_uid]

    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            text=True, bufsize=1)

    # Wait for the "RECORDING" handshake (or an early failure).
    started = False
    while True:
        line = proc.stdout.readline()
        if not line:
            break
        if line.strip() == "RECORDING":
            started = True
            break

    if not started:
        code = proc.wait()
        _report_failure(code)
        return None

    print("→ Recording. Press ENTER (or Ctrl+C) to stop and save.\n")

    # Stopping: a newline on the child's stdin tells it to flush and exit. We must
    # NOT use proc.communicate() — it closes the child's stdin immediately, which
    # the recorder reads as EOF and stops at 0s. Instead keep stdin open and only
    # signal stop when the user presses ENTER (or Ctrl+C → SIGINT).
    def wait_for_enter():
        try:
            input()
        except EOFError:
            pass
        _request_stop(proc)

    threading.Thread(target=wait_for_enter, daemon=True).start()

    # Block reading stdout: the child stays quiet until it stops, then prints the
    # two saved paths. This unblocks once the recorder exits.
    out_lines = []
    try:
        for line in proc.stdout:
            if line.strip():
                out_lines.append(line.strip())
    except KeyboardInterrupt:
        _request_stop(proc)
        for line in proc.stdout:
            if line.strip():
                out_lines.append(line.strip())
    proc.wait()

    if proc.returncode != 0:
        _report_failure(proc.returncode)
        return None

    # The last two non-empty stdout lines are the saved system + mic paths.
    return out_lines[-2:] if len(out_lines) >= 2 else None


def _request_stop(proc):
    try:
        if proc.stdin and not proc.stdin.closed:
            proc.stdin.write("\n")
            proc.stdin.flush()
            proc.stdin.close()
    except (BrokenPipeError, ValueError):
        pass
    try:
        proc.send_signal(signal.SIGINT)
    except ProcessLookupError:
        pass


def _report_failure(code):
    if code == 2:
        print(
            "\nPermission needed. Grant BOTH to your terminal app in\n"
            "System Settings → Privacy & Security:\n"
            "  • Microphone\n"
            "  • Screen Recording  — then FULLY QUIT and reopen your terminal\n"
            "    (the grant only applies after a relaunch), and re-run.",
            file=sys.stderr,
        )
    else:
        print(f"\nRecorder failed (exit {code}). See messages above.", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description="Record meeting audio (ScreenCaptureKit) to ./recordings/")
    parser.add_argument("--list-mics", action="store_true", help="list microphones and exit")
    parser.add_argument("--list", action="store_true", help=argparse.SUPPRESS)  # back-compat alias
    parser.add_argument("--mic", default=None, help="microphone uniqueID (see --list-mics)")
    parser.add_argument("--device", default=None,
                        help=argparse.SUPPRESS)  # deprecated; system audio is automatic now
    parser.add_argument("--name", default="meeting", help="filename suffix (default: meeting)")
    args = parser.parse_args()

    if args.device is not None:
        sys.exit("--device is no longer used (system audio is captured automatically). "
                 "Use --mic <uniqueID> to choose a microphone; see --list-mics.")

    ensure_built()

    if args.list_mics or args.list:
        list_mics()
        return

    os.makedirs(RECORDINGS_DIR, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in args.name)
    base = os.path.join(RECORDINGS_DIR, f"{stamp}_{safe_name}")
    out_system = f"{base}.system.wav"
    out_mic = f"{base}.mic.wav"

    saved = record(out_system, out_mic, args.mic)
    if not saved:
        sys.exit(1)

    sys_rel = os.path.relpath(saved[0])
    mic_rel = os.path.relpath(saved[1])
    print(f"\n→ Saved:\n    ./{sys_rel}\n    ./{mic_rel}")
    print(f"→ Run the pipeline on it:\n    python3 pipeline.py \"{sys_rel}\" \"{mic_rel}\"")


if __name__ == "__main__":
    main()
