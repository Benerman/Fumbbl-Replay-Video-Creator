"""Watch jobs/done/ and deliver the result back to Discord.

The bot keeps an in-memory map of `job_id -> Interaction` so we can
edit the original deferred reply when a job finishes. Even if the
interaction's 15-minute token has expired we ALWAYS post a fresh
message into the invocation channel (the channel id is stored in
the job file itself), so delivery is durable across bot restarts.

On bot startup, the poller also drains any orphaned done/ files
written while the bot was offline — those land as fresh
`channel.send` messages.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path

import discord

from services.common import jobs as jobs_mod
from services.common.paths import DONE_DIR, IN_PROGRESS_DIR
from services.common.rate_limit import UserInFlightTracker

log = logging.getLogger(__name__)

POLL_INTERVAL_S = 2.0
PROGRESS_EDIT_INTERVAL_S = 5.0
INTERACTION_TOKEN_LIFETIME_S = 15 * 60


class Poller:
    """Background task: watches jobs/done/ and surfaces results."""

    def __init__(self, bot: discord.Bot, in_flight: UserInFlightTracker) -> None:
        self._bot = bot
        self._in_flight = in_flight
        self._delivered: set[str] = set()
        self._task: asyncio.Task | None = None
        self._progress_state: dict[str, dict] = {}

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="poller")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        # On startup, mark every existing done/ entry as already-seen so
        # we don't re-deliver. Then poll forward.
        for p in DONE_DIR.glob("*.json"):
            self._delivered.add(p.stem)
        log.info("poller starting; %d historical done jobs seen", len(self._delivered))
        last_progress_edit = 0.0
        while True:
            try:
                self._scan_done()
                if time.monotonic() - last_progress_edit > PROGRESS_EDIT_INTERVAL_S:
                    await self._update_progress()
                    last_progress_edit = time.monotonic()
            except Exception:
                log.exception("poller iteration failed; continuing")
            await asyncio.sleep(POLL_INTERVAL_S)

    def _scan_done(self) -> None:
        for path in DONE_DIR.glob("*.json"):
            if path.stem in self._delivered:
                continue
            self._delivered.add(path.stem)
            try:
                job = jobs_mod.Job.from_path(path)
            except Exception:
                log.exception("failed to read done file %s", path)
                continue
            self._in_flight.release(job.user_id)
            asyncio.create_task(self._deliver(job))

    async def _update_progress(self) -> None:
        """Edit deferred followups with the worker's current phase."""
        for path in IN_PROGRESS_DIR.glob("*.json"):
            try:
                job = jobs_mod.Job.from_path(path)
            except Exception:
                continue
            prev = self._progress_state.get(job.job_id, {})
            if prev.get("status") == job.status and prev.get("phase") == job.phase:
                continue
            self._progress_state[job.job_id] = {"status": job.status, "phase": job.phase}
            await self._edit_followup_safe(
                job, _phase_text(job.status, job.phase, queued=False)
            )

    async def _deliver(self, job: jobs_mod.Job) -> None:
        """Final result delivery.

        Primary path: edit the deferred slash-command reply via the
        interaction webhook. Works without any channel-level Send
        Messages permission because it goes through Discord's
        application webhook, not the channel API.

        Fallback path: channel.send. Only triggered when the followup
        edit fails — e.g., interaction token expired (>15 min), the
        original message was deleted, or the bot was offline when the
        job finished. This is the path that needs channel-level Send
        Messages permission.
        """
        if job.status == "ok":
            body = (
                f"<@{job.user_id}> highlight is up! {job.youtube_url}\n"
                f"Match {job.match_id or '?'} · "
                f"`{job.match_ref}` · render+upload {job.duration_s:.0f}s"
                if job.duration_s
                else f"<@{job.user_id}> highlight is up! {job.youtube_url}"
            )
        else:
            body = (
                f"<@{job.user_id}> highlight job failed at the "
                f"`{job.phase or 'unknown'}` step.\n"
                f"```{(job.message or '').strip()[:1500]}```"
            )

        # Primary delivery: edit the deferred followup.
        if _interaction_still_alive(job):
            if await self._edit_followup_safe(job, body):
                return
            log.info(
                "followup edit failed for job %s; falling back to channel.send",
                job.job_id,
            )
        else:
            log.info(
                "interaction token expired for job %s; using channel.send",
                job.job_id,
            )

        # Fallback only. Discord error codes worth knowing if this
        # 403s:
        #   50001 = Missing Access (bot can't see the channel — View
        #           Channel denied at the channel/category level even
        #           though the role grants it server-wide)
        #   50013 = Missing Permissions (bot can see it but can't
        #           Send Messages — channel-level override denying it)
        try:
            channel = self._bot.get_channel(job.channel_id) or \
                      await self._bot.fetch_channel(job.channel_id)
        except discord.errors.Forbidden as e:
            log.warning(
                "channel %s not accessible for job %s fallback "
                "(code=%s): %s. User did not receive the result. Grant "
                "the bot 'View Channel' + 'Send Messages' on that "
                "channel for restart / >15-min-window delivery.",
                job.channel_id, job.job_id, getattr(e, "code", "?"), e,
            )
            return
        except Exception:
            log.exception("could not fetch channel %s for job %s",
                          job.channel_id, job.job_id)
            return
        try:
            await channel.send(body)
        except discord.errors.Forbidden as e:
            log.warning(
                "channel.send fallback forbidden in channel=%s for "
                "job=%s (code=%s): %s. User did not receive the result. "
                "Grant the bot 'Send Messages' on this channel — "
                "server-wide role perms are layered under channel-level "
                "overrides.",
                job.channel_id, job.job_id, getattr(e, "code", "?"), e,
            )
        except Exception:
            log.exception("could not channel.send for job %s", job.job_id)

    async def _edit_followup_safe(self, job: jobs_mod.Job, content: str) -> bool:
        """Edit the deferred slash-command reply. Returns True on success.

        Uses the webhook endpoint
        `webhooks/{application_id}/{interaction_token}/messages/@original`
        so we don't need to cache the Interaction object across the
        queue boundary.
        """
        try:
            await self._bot.http.edit_webhook_message(
                webhook_id=job.application_id,
                webhook_token=job.interaction_token,
                message_id="@original",
                payload={"content": content},
            )
            return True
        except Exception:
            log.debug("followup edit failed for job %s (token likely expired)",
                      job.job_id)
            return False


def _phase_text(status: str, phase: str, *, queued: bool) -> str:
    if status == "queued":
        return "📋 Queued. Rendering will start shortly…"
    if status == "rendering":
        return "🎬 Rendering the highlight reel…"
    if status == "uploading":
        return "☁️ Uploading to YouTube…"
    if status == "ok":
        return "✅ Done — posting link…"
    if status == "error":
        return f"❌ Failed during `{phase}`."
    return f"… {status}"


def _interaction_still_alive(job: jobs_mod.Job) -> bool:
    requested_at = datetime.fromisoformat(job.requested_at.rstrip("Z"))
    return datetime.utcnow() - requested_at < timedelta(seconds=INTERACTION_TOKEN_LIFETIME_S)
