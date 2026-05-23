"""Worker loop: claim → render → upload → cleanup.

One worker, single-threaded by design. Each iteration claims at
most one job; if the queue is empty we sleep then poll again.
"""

from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path

from services.common import db, jobs as jobs_mod
from services.common.crypto import TokenCrypto
from services.common.paths import OUT_ROOT
from services.worker import youtube_upload
from services.worker.metadata import build as build_metadata
from services.worker.render import render_match

log = logging.getLogger(__name__)

POLL_INTERVAL_S = 2.0


def run(crypto: TokenCrypto, *, settings) -> None:
    """Block forever, processing one job at a time."""
    # On startup: sweep stale in-progress jobs that crashed.
    swept = jobs_mod.sweep_stale()
    if swept:
        log.warning("swept %d stale in-progress jobs on startup", len(swept))

    while True:
        claimed = jobs_mod.claim_next()
        if claimed is None:
            time.sleep(POLL_INTERVAL_S)
            continue
        job, path = claimed
        try:
            _process(job, path, crypto, settings)
        except Exception:
            log.exception("unexpected error processing job %s", job.job_id)
            job.status = "error"
            job.phase = job.phase or "unknown"
            job.message = "Unhandled exception in worker; see logs."
            jobs_mod.complete(path, job)


def _process(job: jobs_mod.Job, path: Path, crypto: TokenCrypto, settings) -> None:
    work_dir = OUT_ROOT / job.job_id
    started = time.time()

    # ---- render --------------------------------------------------------
    log.info("rendering job=%s match_ref=%s", job.job_id, job.match_ref)
    job.status = "rendering"
    job.phase = "render"
    jobs_mod.update(path, status="rendering", phase="render")
    try:
        result = render_match(job.match_ref, work_dir)
    except Exception as e:
        log.exception("render failed for job=%s", job.job_id)
        job.status = "error"
        job.phase = "render"
        job.message = str(e)[:1500]
        jobs_mod.complete(path, job)
        return

    # ---- credentials + upload -----------------------------------------
    log.info(
        "uploading job=%s regular=%s short=%s",
        job.job_id, result.regular_mp4, result.short_mp4,
    )
    job.status = "uploading"
    job.phase = "upload"
    jobs_mod.update(path, status="uploading", phase="upload")
    try:
        creds, used_default = youtube_upload.load_credentials_for_guild(
            job.guild_id, crypto
        )
    except Exception as e:
        log.exception("could not load YT credentials for guild=%s", job.guild_id)
        job.status = "error"
        job.phase = "credentials"
        job.message = str(e)[:1500]
        jobs_mod.complete(path, job)
        return

    # Regular 16:9 upload first. Failing it fails the whole job — this
    # is the primary deliverable and what we dedup against.
    regular_meta = build_metadata(
        result.analysis, job.match_id, job.replay_id, variant="regular"
    )
    try:
        regular_upload = youtube_upload.upload_video(
            creds=creds,
            mp4_path=result.regular_mp4,
            title=regular_meta.title,
            description=regular_meta.description,
            tags=regular_meta.tags,
            privacy=settings.youtube_privacy,
            category_id=settings.youtube_category_id,
        )
    except Exception as e:
        log.exception("regular upload failed for job=%s", job.job_id)
        job.status = "error"
        job.phase = "upload"
        job.message = str(e)[:1500]
        jobs_mod.complete(path, job)
        return

    # Short 9:16 upload is best-effort. If it fails, the job is still
    # ok — the user got the main video. Capture the failure reason so
    # we can surface it in the Discord followup.
    jobs_mod.update(path, phase="upload_short")
    short_url: str | None = None
    short_video_id: str | None = None
    short_error: str | None = None
    short_meta = build_metadata(
        result.analysis, job.match_id, job.replay_id, variant="short"
    )
    try:
        short_upload = youtube_upload.upload_video(
            creds=creds,
            mp4_path=result.short_mp4,
            title=short_meta.title,
            description=short_meta.description,
            tags=short_meta.tags,
            privacy=settings.youtube_privacy,
            category_id=settings.youtube_category_id,
        )
        short_url = short_upload.url
        short_video_id = short_upload.video_id
    except Exception as e:
        log.exception("short upload failed for job=%s (regular already uploaded)",
                      job.job_id)
        short_error = str(e)[:500]

    # ---- dedup record + cleanup ---------------------------------------
    try:
        db.record_processed(
            guild_id=job.guild_id,
            match_id=job.match_id,
            replay_id=job.replay_id,
            youtube_video_id=regular_upload.video_id,
            youtube_url=regular_upload.url,
            used_default_creds=used_default,
            youtube_short_video_id=short_video_id,
            youtube_short_url=short_url,
        )
    except Exception:
        log.exception("could not record_processed for job=%s (continuing)", job.job_id)

    try:
        shutil.rmtree(work_dir)
        log.debug("cleaned work_dir %s", work_dir)
    except FileNotFoundError:
        pass
    except Exception:
        log.exception("cleanup failed for work_dir %s", work_dir)

    job.status = "ok"
    job.phase = "done"
    job.youtube_url = regular_upload.url
    job.youtube_video_id = regular_upload.video_id
    job.youtube_short_url = short_url
    job.youtube_short_video_id = short_video_id
    job.short_upload_error = short_error
    job.duration_s = round(time.time() - started, 1)
    jobs_mod.complete(path, job)
