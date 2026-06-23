#!/usr/bin/env python3
"""
Offline backend test for Gotcha's multi-user server.

FREE to run: monkeypatches pipeline.transcribe_two_track / _interpret_and_save so
NO Sarvam/Gemini (paid) calls happen. Verifies auth, upload + WAV validation,
per-user namespacing/isolation, the usage ledger, and the cost cap.

    python3 webapp/test_backend.py
"""
import os
import sys
import json
import time
import wave
import tempfile

TMP = tempfile.mkdtemp(prefix="gotcha_test_")
USERS = os.path.join(TMP, "users.json")
with open(USERS, "w") as f:
    json.dump({
        "alice-tok": {"user_id": "alice", "display_name": "Alice"},
        "bob-tok":   {"user_id": "bob",   "display_name": "Bob"},
        "zero-tok":  {"user_id": "zero",  "display_name": "Zero", "cap_minutes": 0},
    }, f)

os.environ["GOTCHA_USERS_FILE"] = USERS
os.environ["GOTCHA_DATA_DIR"] = TMP

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fastapi.testclient import TestClient  # noqa: E402
import webapp.server as server  # noqa: E402

# --- monkeypatch the paid pipeline calls -----------------------------------
def _fake_transcribe(system_path, mic_path, *, cfg=None):
    return [{"speaker_id": cfg.your_name, "t": 1.0, "text": "hi"},
            {"speaker_id": "Other", "t": 2.0, "text": "do the thing [2.0s]"}]


def _fake_interpret_and_save(entries, two_track, out_dir, base, *, cfg=None):
    with open(os.path.join(out_dir, f"{base}.report.md"), "w", encoding="utf-8") as f:
        f.write(f"### 1. Meeting summary\nHi {cfg.your_name} — do the thing [2.0s]\n")


server.pipeline.transcribe_two_track = _fake_transcribe
server.pipeline._interpret_and_save = _fake_interpret_and_save

client = TestClient(server.app)
PASS, FAIL = [], []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("  ok  " if cond else " FAIL ") + name)


def make_wav(path, secs=0.2, rate=48000):
    with wave.open(path, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(secs * rate))


def H(tok):
    return {"Authorization": f"Bearer {tok}"}


def upload(tok, name="standup"):
    sysp, micp = os.path.join(TMP, "s.wav"), os.path.join(TMP, "m.wav")
    make_wav(sysp); make_wav(micp)
    with open(sysp, "rb") as sf, open(micp, "rb") as mf:
        return client.post("/api/upload",
                           files={"system": ("a.system.wav", sf, "audio/wav"),
                                  "mic": ("a.mic.wav", mf, "audio/wav")},
                           data={"name": name}, headers=H(tok))


def wait_done(tok, base, timeout=10):
    for _ in range(timeout * 10):
        st = client.get(f"/api/jobs/{base}", headers=H(tok)).json()["state"]
        if st in ("done", "error"):
            return st
        time.sleep(0.1)
    return "timeout"


print("AUTH")
check("no token → 401", client.get("/api/meetings").status_code == 401)
check("bad token → 403", client.get("/api/meetings", headers=H("nope")).status_code == 403)
check("valid token → 200", client.get("/api/meetings", headers=H("alice-tok")).status_code == 200)

print("UPLOAD + ISOLATION")
r = upload("alice-tok")
check("upload accepted", r.status_code == 200)
base = r.json().get("base", "")
check("job completes", wait_done("alice-tok", base) == "done")

a_list = client.get("/api/meetings", headers=H("alice-tok")).json()["meetings"]
b_list = client.get("/api/meetings", headers=H("bob-tok")).json()["meetings"]
check("alice sees her meeting", any(m["base"] == base for m in a_list))
check("bob sees nothing (isolation)", len(b_list) == 0)

check("alice reads her report", client.get(f"/api/meetings/{base}", headers=H("alice-tok")).status_code == 200)
check("bob cannot read alice's base (404)", client.get(f"/api/meetings/{base}", headers=H("bob-tok")).status_code == 404)
check("alice can play system track", client.get(f"/api/audio/{base}/system", headers=H("alice-tok")).status_code == 200)

print("VALIDATION + CAP")
bad = os.path.join(TMP, "bad.wav")
with open(bad, "w") as f:
    f.write("not a wav")
with open(bad, "rb") as bf, open(bad, "rb") as bf2:
    rb = client.post("/api/upload",
                     files={"system": ("x.system.wav", bf, "audio/wav"),
                            "mic": ("x.mic.wav", bf2, "audio/wav")},
                     data={"name": "junk"}, headers=H("alice-tok"))
check("non-WAV rejected (400)", rb.status_code == 400)
check("zero-cap user blocked (429)", upload("zero-tok").status_code == 429)
check("path traversal collapses to 404", client.get("/api/meetings/..%2f..%2fetc", headers=H("alice-tok")).status_code == 404)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
