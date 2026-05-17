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
    log.info("uploading job=%s mp4=%s", job.job_id, result.mp4_path)
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

    metadata = build_metadata(result.analysis, job.match_id, job.replay_id)
    try:
        upload = youtube_upload.upload_video(
            creds=creds,
            mp4_path=result.mp4_path,
            title=metadata.title,
            description=metadata.description,
            tags=metadata.tags,
            privacy=settings.youtube_privacy,
            category_id=settings.youtube_category_id,
        )
    except Exception as e:
        log.exception("upload failed for job=%s", job.job_id)
        job.status = "error"
        job.phase = "upload"
        job.message = str(e)[:1500]
        jobs_mod.complete(path, job)
        return

    # ---- dedup record + cleanup ---------------------------------------
    try:
        db.record_processed(
            guild_id=job.guild_id,
            match_id=job.match_id,
            replay_id=job.replay_id,
            youtube_video_id=upload.video_id,
            youtube_url=upload.url,
            used_default_creds=used_default,
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
    job.youtube_url = upload.url
    job.youtube_video_id = upload.video_id
    job.duration_s = round(time.time() - started, 1)
    jobs_mod.complete(path, job)
