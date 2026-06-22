#!/usr/bin/env python3
"""
VAD mic-track trimming — strip silence before transcription, keep timestamps.
=============================================================================
The mic track is mostly silence (you listen most of a meeting, speak ~10-20%).
Sending the full track to Sarvam bills you for all that silence. This module
trims it locally with WebRTC VAD and — crucially — keeps a map back to the
ORIGINAL meeting timeline so the trimmed transcript's timestamps can be remapped
and still align with the (untrimmed) system track.

Pure stdlib + webrtcvad: mic.wav is 48kHz mono PCM16, which webrtcvad supports
natively, so no numpy/soundfile/resampling needed.

Usage in the pipeline:
    trimmed_path, seg_map = trim_silence("meeting.mic.wav")
    if trimmed_path:
        entries = transcribe(trimmed_path, ...)        # timestamps in TRIMMED time
        for e in entries: e["t"] = remap(e["t"], seg_map)   # -> ORIGINAL time
        os.remove(trimmed_path)
    else:
        entries = transcribe("meeting.mic.wav", ...)   # fallback: full track
"""

import os
import wave
import tempfile

# webrtcvad needs frames of exactly 10/20/30ms of 16-bit mono PCM at 8/16/32/48kHz.
FRAME_MS = 30
SUPPORTED_RATES = (8000, 16000, 32000, 48000)


def trim_silence(wav_path, aggressiveness=2, pad_ms=250, min_gap_ms=300, gap_ms=700):
    """Return (trimmed_wav_path, seg_map), or (None, None) to signal the caller
    to fall back to the full track (no VAD lib, unsupported format, or no speech).

    seg_map is a list of dicts {orig_start, orig_end, trim_start, trim_end} in
    seconds, mapping positions in the trimmed file back to the original file.

    gap_ms of silence is inserted BETWEEN kept segments (not removed entirely) so
    the ASR still segments distinct utterances separately — preserving per-utterance
    timestamps — while the bulk of the dead air is still stripped.
    """
    try:
        import webrtcvad
    except ImportError:
        return None, None

    with wave.open(wav_path, "rb") as w:
        channels = w.getnchannels()
        width = w.getsampwidth()
        rate = w.getframerate()
        n = w.getnframes()
        pcm = w.readframes(n)

    # webrtcvad constraints: mono, 16-bit, supported rate.
    if channels != 1 or width != 2 or rate not in SUPPORTED_RATES:
        return None, None

    vad = webrtcvad.Vad(aggressiveness)
    bytes_per_frame = int(rate * (FRAME_MS / 1000.0)) * width  # samples * 2 bytes
    frame_secs = FRAME_MS / 1000.0

    # 1) Classify each frame as voiced/unvoiced.
    voiced_flags = []
    offset = 0
    while offset + bytes_per_frame <= len(pcm):
        frame = pcm[offset:offset + bytes_per_frame]
        voiced_flags.append(vad.is_speech(frame, rate))
        offset += bytes_per_frame
    if not any(voiced_flags):
        return None, None

    # 2) Collapse runs of voiced frames into [start_frame, end_frame) regions,
    #    pad each by a collar so we don't clip word onsets/offsets.
    pad_frames = max(1, int(pad_ms / FRAME_MS))
    regions = []  # (start_frame, end_frame) exclusive end
    in_run = False
    run_start = 0
    for i, v in enumerate(voiced_flags):
        if v and not in_run:
            in_run = True
            run_start = i
        elif not v and in_run:
            in_run = False
            regions.append((run_start, i))
    if in_run:
        regions.append((run_start, len(voiced_flags)))

    total_frames = len(voiced_flags)
    padded = []
    for s, e in regions:
        padded.append((max(0, s - pad_frames), min(total_frames, e + pad_frames)))

    # 3) Merge regions whose gap is smaller than min_gap (avoid over-fragmenting).
    gap_frames = max(0, int(min_gap_ms / FRAME_MS))
    merged = []
    for s, e in padded:
        if merged and s - merged[-1][1] <= gap_frames:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))

    # 4) Concatenate the byte ranges (with a short silence inserted between
    #    segments so the ASR keeps them as separate utterances) and build the
    #    original<->trimmed time map.
    gap_secs = gap_ms / 1000.0
    gap_bytes = b"\x00" * (int(rate * gap_secs) * width)
    seg_map = []
    chunks = []
    trim_cursor = 0.0
    for idx, (s, e) in enumerate(merged):
        if idx > 0:
            chunks.append(gap_bytes)      # inter-utterance silence
            trim_cursor += gap_secs
        b0 = s * bytes_per_frame
        b1 = e * bytes_per_frame
        chunks.append(pcm[b0:b1])
        dur = (e - s) * frame_secs
        seg_map.append({
            "orig_start": s * frame_secs,
            "orig_end": e * frame_secs,
            "trim_start": trim_cursor,
            "trim_end": trim_cursor + dur,
        })
        trim_cursor += dur

    fd, trimmed_path = tempfile.mkstemp(prefix="mic_trim_", suffix=".wav")
    os.close(fd)
    with wave.open(trimmed_path, "wb") as out:
        out.setnchannels(channels)
        out.setsampwidth(width)
        out.setframerate(rate)
        out.writeframes(b"".join(chunks))

    return trimmed_path, seg_map


def remap(t_trimmed, seg_map):
    """Map a timestamp in trimmed-audio time back to original-meeting time.

    Handles timestamps landing inside an inter-segment silence gap by snapping
    them to the end of the preceding utterance (rather than mis-mapping)."""
    if not seg_map:
        return t_trimmed
    # Before the first segment → start of the first utterance.
    if t_trimmed < seg_map[0]["trim_start"]:
        return seg_map[0]["orig_start"]
    # Pick the last segment that starts at or before t_trimmed.
    chosen = seg_map[0]
    for seg in seg_map:
        if seg["trim_start"] <= t_trimmed:
            chosen = seg
        else:
            break
    mapped = chosen["orig_start"] + (t_trimmed - chosen["trim_start"])
    # If t_trimmed fell in the gap after `chosen`, clamp to that utterance's end.
    return min(mapped, chosen["orig_end"])


def summarize(seg_map, original_secs):
    """Human-readable trim summary for logging."""
    kept = seg_map[-1]["trim_end"] if seg_map else 0.0
    pct = (kept / original_secs * 100) if original_secs else 0.0
    return f"{kept:.1f}s of {original_secs:.1f}s kept ({pct:.0f}%)"
