# Single image used for both the bot and the worker. The two
# docker-compose services differ only in `command:`. ffmpeg is the
# heaviest system dep we need — it's used by mix/compose inside
# fumbbl_replay.

FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# System packages: ffmpeg for video encoding, fonts for the renderer's
# text overlays, libsndfile for kokoro (optional but cheap), build
# essentials only during install of any wheels needing compilation.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        fonts-dejavu \
        libsndfile1 \
        ca-certificates \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first so the image cache reuses them across
# source changes.
COPY requirements.txt requirements-services.txt /app/
RUN pip install -r requirements.txt -r requirements-services.txt

# Application source.
COPY fumbbl_replay /app/fumbbl_replay
COPY services /app/services

# Default env. Override these in the compose env_file. The volumes
# mounted from the host (data, jobs, out) live under /app.
# FUMBBL_REPLAY_CACHE points inside /app/data so FUMBBL position
# icons, pitch backgrounds, dice sprites, FFB SFX, and kokoro TTS
# models survive container restarts — otherwise every cold start
# re-downloads ~50+ MB of static assets.
ENV JOBS_ROOT=/app/jobs \
    OUT_ROOT=/app/out \
    DATA_ROOT=/app/data \
    FUMBBL_REPLAY_CACHE=/app/data/cache \
    FUMBBL_KOKORO_CACHE=/app/data/cache/kokoro

# Each compose service overrides this `command:` with either
# `python -m services.bot` or `python -m services.worker`.
CMD ["python", "-m", "services.worker"]
