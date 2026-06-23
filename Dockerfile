# Gotcha backend — the shared, OS-agnostic cloud server (holds keys, runs the
# pipeline, stores per-user reports/audio). Capture happens on the client; this
# image never records. Deps are pure-Python wheels (sarvamai, google-genai,
# webrtcvad-wheels, fastapi/uvicorn) so no build toolchain / apt packages needed.
FROM python:3.12-slim

WORKDIR /app

# Deps first for layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# The backend (webapp) + the shared pipeline modules it imports at runtime:
#   pipeline.py        transcribe_two_track() + interpret()
#   mixdown.py         lazy Mix track for playback (webapp/server.py imports it)
#   vad.py             mic silence-trim (pipeline.py imports it lazily)
COPY pipeline.py mixdown.py vad.py ./
COPY webapp/ ./webapp/

# Per-user reports/audio/usage live under GOTCHA_DATA_DIR — mount this as a
# (provider-encrypted) volume at deploy. users.json lives there too.
ENV GOTCHA_DATA_DIR=/data \
    GOTCHA_USERS_FILE=/data/users.json \
    PYTHONUNBUFFERED=1
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000

# Single worker on purpose: the backend runs ONE background worker thread that
# serializes Sarvam jobs (the paid chokepoint) and keeps in-process job state.
# Scaling out would need a shared queue first — not for the alpha.
CMD ["uvicorn", "webapp.server:app", "--host", "0.0.0.0", "--port", "8000"]
