#!/usr/bin/env python3
"""
RecorderSession — non-blocking start/stop wrapper around the `mac-recorder` CLI.
================================================================================
record_meeting.py drives the Swift recorder with a BLOCKING flow (spawn → wait for
ENTER → stop). The web server needs the same capture but split into two calls:
`start()` returns immediately while recording continues in the background, and a
later `stop()` flushes and returns the two saved WAV paths.

To avoid a second copy of the fragile recorder handshake, this reuses the validated
helpers from record_meeting.py (`ensure_built`, `BINARY`, `RECORDINGS_DIR`,
`_request_stop`, `_report_failure`). record_meeting.py itself is left untouched.

Key invariants carried over from record_meeting.record() (don't change blindly):
  • Wait for the child's "RECORDING" handshake line before treating it as started.
  • Never close the child's stdin early — that reads as EOF and stops it at 0s.
    Stopping is `_request_stop`: a newline on stdin, then SIGINT.
  • The recorder stays quiet until stopped, then prints the two saved paths; a
    background reader thread drains stdout so the pipe never blocks the child.
"""

import os
import time
import datetime
import threading
import subprocess

import record_meeting as rm


class RecorderError(RuntimeError):
    """Raised on a failed start/stop; message is safe to surface to the UI."""


class RecorderSession:
    """Single active recording at a time (this is a personal single-user tool)."""

    def __init__(self):
        self._proc = None
        self._reader = None
        self._lines = []
        self._lock = threading.Lock()
        self.base = None
        self.system_path = None
        self.mic_path = None
        self.started_at = None

    @property
    def recording(self):
        return self._proc is not None and self._proc.poll() is None

    def elapsed(self):
        return (time.time() - self.started_at) if (self.recording and self.started_at) else 0.0

    def status(self):
        return {"recording": self.recording, "base": self.base if self.recording else None,
                "elapsed": round(self.elapsed(), 1)}

    def start(self, name="meeting", mic_uid=None):
        """Spawn the recorder and block only until the RECORDING handshake; then
        return the meeting `base` (filename stem shared by both tracks)."""
        with self._lock:
            if self.recording:
                raise RecorderError("A recording is already in progress.")

            rm.ensure_built()
            os.makedirs(rm.RECORDINGS_DIR, exist_ok=True)
            stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in (name or "meeting"))
            base = f"{stamp}_{safe}"
            base_path = os.path.join(rm.RECORDINGS_DIR, base)
            system_path = base_path + ".system.wav"
            mic_path = base_path + ".mic.wav"

            cmd = [rm.BINARY, "--out-system", system_path, "--out-mic", mic_path]
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
                raise RecorderError(self._failure_message(code))

            self._proc = proc
            self._lines = []
            self.base = base
            self.system_path = system_path
            self.mic_path = mic_path
            self.started_at = time.time()

            # Drain stdout in the background: the recorder stays silent until it
            # stops, then prints the saved paths. Keeping the pipe read prevents
            # the child from blocking on a full stdout buffer.
            self._reader = threading.Thread(target=self._drain, args=(proc,), daemon=True)
            self._reader.start()
            return base

    def _drain(self, proc):
        try:
            for line in proc.stdout:
                if line.strip():
                    self._lines.append(line.strip())
        except (ValueError, OSError):
            pass  # pipe closed during shutdown

    def stop(self):
        """Signal the recorder to flush + exit, then return (base, system, mic)."""
        with self._lock:
            if self._proc is None:
                raise RecorderError("No active recording to stop.")
            proc, base = self._proc, self.base
            system_path, mic_path = self.system_path, self.mic_path

            rm._request_stop(proc)
            proc.wait()
            if self._reader:
                self._reader.join(timeout=5)
            code = proc.returncode

            self._proc = None
            self._reader = None
            self.base = None
            self.started_at = None

            if code != 0:
                raise RecorderError(self._failure_message(code))
            # We passed --out-system/--out-mic explicitly, so the files are exactly
            # at our known paths (no need to parse the echoed stdout lines).
            return base, system_path, mic_path

    @staticmethod
    def _failure_message(code):
        # Log the full guidance (incl. the permission steps) to the server console…
        rm._report_failure(code)
        # …and return a concise message for the HTTP/UI layer.
        if code == 2:
            return ("Permission needed: grant Microphone and Screen Recording to the "
                    "app running this server (System Settings → Privacy & Security), "
                    "then fully quit and relaunch it. See server log for details.")
        return f"Recorder failed (exit {code}). See server log for details."
