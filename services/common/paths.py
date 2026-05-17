"""Filesystem paths used by both the bot and the worker.

All four roots are env-overridable so the same code runs locally
(./jobs, ./data, ./out) and inside the docker compose stack
(/app/jobs, /app/data, /app/out). Default to relative paths so a
contributor cloning the repo can `python -m services.bot` without
setting any envs first.
"""

from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

JOBS_ROOT = Path(os.environ.get("JOBS_ROOT", REPO_ROOT / "jobs"))
OUT_ROOT = Path(os.environ.get("OUT_ROOT", REPO_ROOT / "out"))
DATA_ROOT = Path(os.environ.get("DATA_ROOT", REPO_ROOT / "data"))

QUEUE_DIR = JOBS_ROOT / "queue"
IN_PROGRESS_DIR = JOBS_ROOT / "in-progress"
DONE_DIR = JOBS_ROOT / "done"

SQLITE_PATH = DATA_ROOT / "app.sqlite3"


def ensure_dirs() -> None:
    """Idempotent: create the dir tree the services need."""
    for d in (QUEUE_DIR, IN_PROGRESS_DIR, DONE_DIR, DATA_ROOT, OUT_ROOT):
        d.mkdir(parents=True, exist_ok=True)
