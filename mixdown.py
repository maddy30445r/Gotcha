#!/usr/bin/env python3
"""
mixdown — combine the two capture tracks into ONE playback WAV.
===============================================================
The pipeline keeps system (the call/others) and mic (you) as SEPARATE files for
perfect speaker separation during transcription (see memory decision-two-track-audio).
But for *listening back* you want to hear the actual moment — both voices together.
This module sums the two tracks into `{base}.mix.wav` purely for playback; it does
NOT touch the transcription path, so the two-track separation principle is preserved.

Both tracks are mono / 16-bit PCM recorded simultaneously, but they can differ in
sample rate: the system track is 48kHz while the mic (macOS Voice Processing) is
device-dependent and often 24kHz. We resample the mic up to the system rate before
summing so both line up in time — otherwise the mic plays back fast / high-pitched.
Stdlib only (`wave` + `array`) — deliberately NOT `audioop`, which is deprecated and
removed in Python 3.13.
"""

import wave
from array import array


def _read_int16(path):
    with wave.open(path, "rb") as w:
        params = (w.getnchannels(), w.getframerate(), w.getsampwidth())
        frames = w.readframes(w.getnframes())
    samples = array("h")          # signed 16-bit
    samples.frombytes(frames)
    return samples, params


def _resample_int16(samples, src_rate, dst_rate):
    """Linear-interpolation resample of a mono int16 buffer. Adequate for voice
    playback and dependency-free; the mix is cached so this runs once per meeting."""
    if src_rate == dst_rate or not len(samples):
        return samples
    n_out = int(len(samples) * dst_rate / src_rate)
    out = array("h", bytes(2 * n_out))
    ratio = src_rate / dst_rate
    last = len(samples) - 1
    for i in range(n_out):
        pos = i * ratio
        i0 = int(pos)
        frac = pos - i0
        s0 = samples[i0]
        s1 = samples[i0 + 1] if i0 < last else s0
        out[i] = int(s0 + (s1 - s0) * frac)
    return out


def mix_tracks(system_path, mic_path, out_path):
    """Sum two mono 16-bit PCM WAVs into out_path (clamped). Returns out_path.

    The shorter track is padded with trailing silence so both line up from t=0.
    Raises if the inputs aren't the expected mono/16-bit format (we don't guess)."""
    a, pa = _read_int16(system_path)
    b, pb = _read_int16(mic_path)

    ch_a, rate_a, width_a = pa
    ch_b, rate_b, width_b = pb
    if width_a != 2 or width_b != 2:
        raise ValueError("mix_tracks expects 16-bit PCM tracks")
    if ch_a != 1 or ch_b != 1:
        raise ValueError("mix_tracks expects mono tracks")

    # Bring the mic up to the system rate so the sample-aligned sum is time-correct.
    b = _resample_int16(b, rate_b, rate_a)

    n = max(len(a), len(b))
    out = array("h", bytes(2 * n))   # zero-filled (silence) of the longer length
    for i in range(len(a)):
        out[i] = a[i]
    for i in range(len(b)):
        s = out[i] + b[i]
        # Clamp to int16 range to avoid overflow wrap (audible clicks).
        out[i] = 32767 if s > 32767 else (-32768 if s < -32768 else s)

    with wave.open(out_path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate_a)
        w.writeframes(out.tobytes())
    return out_path
