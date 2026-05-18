"""Filesystem job-queue helpers shared by bot and worker.

Three directories under JOBS_ROOT: queue/, in-progress/, done/. A job
is a UUID-named JSON file that gets renamed (atomically on POSIX)
from queue -> in-progress -> done as it moves through the pipeline.
Worker only ever touches one file at a time so we don't need
distributed-lock infrastructure.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .paths import DONE_DIR, IN_PROGRESS_DIR, QUEUE_DIR, ensure_dirs

log = logging.getLogger(__name__)


@dataclass
class Job:
    """Shape persisted as queue/<job_id>.json."""
    job_id: str
    match_ref: str                  # raw user input — already sanitized
    match_id: int | None            # filled in by sanitizer.resolve()
    replay_id: int | None
    guild_id: int
    channel_id: int                 # invocation channel — where we post the result
    user_id: int
    interaction_token: str          # for the 15-min followup window
    application_id: int
    requested_at: str
    # Mutable per-phase fields (worker writes these while in-progress/):
    status: str = "queued"          # queued | rendering | uploading | ok | error
    phase: str = ""
    message: str = ""
    youtube_url: str | None = None
    youtube_video_id: str | None = None
    # Second upload: 9:16 Shorts variant. Best-effort — if the regular
    # upload succeeded but the Short failed, the job is still "ok" and
    # these stay None. short_upload_error captures the failure reason.
    youtube_short_url: str | None = None
    youtube_short_video_id: str | None = None
    short_upload_error: str | None = None
    duration_s: float | None = None

    @classmethod
    def new(
        cls,
        *,
        match_ref: str,
        match_id: int | None,
        replay_id: int | None,
        guild_id: int,
        channel_id: int,
        user_id: int,
        interaction_token: str,
        application_id: int,
    ) -> "Job":
        return cls(
            job_id=str(uuid.uuid4()),
            match_ref=match_ref,
            match_id=match_id,
            replay_id=replay_id,
            guild_id=guild_id,
            channel_id=channel_id,
            user_id=user_id,
            interaction_token=interaction_token,
            application_id=application_id,
            requested_at=datetime.utcnow().isoformat(timespec="seconds") + "Z",
        )

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)

    @classmethod
    def from_path(cls, path: Path) -> "Job":
        data = json.loads(path.read_text())
        return cls(**data)


def enqueue(job: Job) -> Path:
    """Write the job into queue/. Returns the path."""
    ensure_dirs()
    path = QUEUE_DIR / f"{job.job_id}.json"
    path.write_text(job.to_json())
    log.info("enqueued job %s match_ref=%s", job.job_id, job.match_ref)
    return path


def claim_next() -> tuple[Job, Path] | None:
    """Atomically claim the oldest queue file by moving it to in-progress/.

    Returns (job, path) or None if the queue is empty. Concurrency-safe
    via os.rename's atomicity on POSIX: if two workers race, exactly
    one rename succeeds and the loser sees FileNotFoundError.
    """
    ensure_dirs()
    candidates = sorted(QUEUE_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime)
    for src in candidates:
        dst = IN_PROGRESS_DIR / src.name
        try:
            os.rename(src, dst)
        except FileNotFoundError:
            continue
        return Job.from_path(dst), dst
    return None


def update(path: Path, **fields: Any) -> Job:
    """Patch an in-progress job file with new status/phase/message."""
    job = Job.from_path(path)
    for k, v in fields.items():
        setattr(job, k, v)
    path.write_text(job.to_json())
    return job


def complete(path: Path, job: Job) -> Path:
    """Move an in-progress job to done/ with its final state."""
    dst = DONE_DIR / path.name
    path.write_text(job.to_json())
    os.rename(path, dst)
    log.info("completed job %s status=%s", job.job_id, job.status)
    return dst


def in_progress_jobs() -> list[Job]:
    return [Job.from_path(p) for p in IN_PROGRESS_DIR.glob("*.json")]


def sweep_stale(max_age_seconds: int = 1800) -> list[str]:
    """Move in-progress jobs older than max_age_seconds to done/ as crashed.
    Called by the worker on startup."""
    import time
    moved: list[str] = []
    now = time.time()
    for p in IN_PROGRESS_DIR.glob("*.json"):
        if now - p.stat().st_mtime > max_age_seconds:
            job = Job.from_path(p)
            job.status = "error"
            job.phase = "worker_crash"
            job.message = "Worker died while processing this job; auto-marked crashed on restart."
            complete(p, job)
            moved.append(job.job_id)
    return moved
