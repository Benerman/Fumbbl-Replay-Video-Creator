"""Slash-command definitions.

Two top-level commands:

  /generate-highlight <match-ref>
      Validate, dedup-check, rate-limit, enqueue a job. Replies to the
      user with status; the poller delivers the final result.

  /highlight-config <subcommand>
      Admin (Manage Server) commands to set / show / reset the
      per-guild YouTube override.
"""

# NB: do NOT add `from __future__ import annotations` to this module.
# py-cord 2.x inspects each slash-command parameter's annotation
# (`inspect.signature(func).parameters[...].annotation`) to figure
# out the Discord option type. With future-style annotations every
# annotation becomes a STRING (`"str"`) instead of the actual class,
# and py-cord's `issubclass(annotation, OptionClass)` check raises
# `TypeError: issubclass() arg 1 must be a class`. Keeping the real
# class on the annotation makes the bot pick the right option type
# automatically.

import logging
from typing import TYPE_CHECKING

import discord
from discord import option, slash_command
from discord.commands import SlashCommandGroup
from discord.ext import commands

from services.bot.sanitizer import sanitize_match_ref
from services.common import db, jobs as jobs_mod
from services.common.rate_limit import (
    UserInFlightTracker,
    check_guild_rate,
    record_guild_invocation,
)

if TYPE_CHECKING:
    from services.bot.oauth_handler import OAuthHandler
    from services.common.config import Settings

log = logging.getLogger(__name__)


def register(
    bot: discord.Bot,
    settings: "Settings",
    in_flight: UserInFlightTracker,
    oauth: "OAuthHandler",
) -> None:
    """Bind the slash commands onto the bot."""

    @bot.slash_command(
        name="generate-highlight",
        description="Render a FUMBBL match into a YouTube highlight reel.",
    )
    @option(
        "match_ref",
        input_type=str,
        description="FUMBBL match id (e.g. 4700552) or full match/replay URL.",
    )
    async def generate_highlight(ctx: discord.ApplicationContext, match_ref: str) -> None:
        if ctx.guild is None:
            await ctx.respond(
                "This command only works inside a server.", ephemeral=True
            )
            return

        # 1. Sanitize + resolve.
        result = sanitize_match_ref(match_ref)
        if not result.ok:
            await ctx.respond(result.reason, ephemeral=True)
            return
        resolved = result.resolved
        match_id = resolved.match_id if resolved else None
        replay_id = resolved.replay_id if resolved else None

        # 2. Dedup check.
        existing = db.find_processed(ctx.guild.id, match_id, replay_id)
        if existing is not None:
            await ctx.respond(
                f"This match has already been processed: {existing['youtube_url']}",
                ephemeral=True,
            )
            return

        # 3. Per-guild rate limit.
        decision = check_guild_rate(
            ctx.guild.id, settings.rate_limit_per_guild_per_10min
        )
        if not decision.allowed:
            await ctx.respond(decision.reason, ephemeral=True)
            return

        # 4. Per-user in-flight.
        if not in_flight.try_claim(ctx.author.id):
            await ctx.respond(
                "You already have a highlight job running. Wait for it to "
                "finish before requesting another.",
                ephemeral=True,
            )
            return

        # 5. Defer + enqueue.
        await ctx.defer()  # public ack — bot shows "thinking..."
        try:
            record_guild_invocation(ctx.guild.id, ctx.author.id)
            job = jobs_mod.Job.new(
                match_ref=result.cleaned,
                match_id=match_id,
                replay_id=replay_id,
                guild_id=ctx.guild.id,
                channel_id=ctx.channel.id,
                user_id=ctx.author.id,
                interaction_token=ctx.interaction.token,
                application_id=int(settings.discord_application_id),
            )
            jobs_mod.enqueue(job)
        except Exception:
            in_flight.release(ctx.author.id)
            log.exception("failed to enqueue job for user=%s", ctx.author.id)
            await ctx.respond(
                "Something went wrong while queueing your job. Try again in a moment.",
                ephemeral=True,
            )
            return
        await ctx.followup.send(
            "📋 Queued. Rendering will start shortly…"
        )

    # /highlight-config group ----------------------------------------------

    config = SlashCommandGroup(
        "highlight-config",
        "Per-server highlight bot settings (admin only).",
    )

    @config.command(name="show", description="Show this server's YouTube override (if any).")
    async def show_(ctx: discord.ApplicationContext) -> None:
        if ctx.guild is None:
            await ctx.respond("Server only.", ephemeral=True)
            return
        if not ctx.author.guild_permissions.manage_guild:
            await ctx.respond("You need Manage Server permission.", ephemeral=True)
            return
        row = db.get_guild_config(ctx.guild.id)
        if row is None or row["yt_refresh_token_encrypted"] is None:
            await ctx.respond(
                "This server is using the bot's default YouTube channel. "
                "Run `/highlight-config set-youtube` to link your own.",
                ephemeral=True,
            )
            return
        await ctx.respond(
            f"Linked YouTube channel id: `{row['yt_channel_id'] or 'unknown'}` "
            f"(configured by <@{row['set_by_user_id']}> on {row['set_at']}).",
            ephemeral=True,
        )

    @config.command(
        name="set-youtube",
        description="Link this server's own YouTube channel for uploads.",
    )
    async def set_youtube(ctx: discord.ApplicationContext) -> None:
        if ctx.guild is None:
            await ctx.respond("Server only.", ephemeral=True)
            return
        if not ctx.author.guild_permissions.manage_guild:
            await ctx.respond("You need Manage Server permission.", ephemeral=True)
            return
        try:
            state, auth_url = oauth.begin(ctx.guild.id, ctx.author.id)
        except RuntimeError as e:
            await ctx.respond(f"OAuth setup error: {e}", ephemeral=True)
            return
        await ctx.respond(
            "Click here to authorize the bot to upload to your YouTube channel "
            f"(valid for 10 minutes): {auth_url}\n\n"
            "Sign in with the **brand account** for the YouTube channel where "
            "you want highlights uploaded. After authorizing, you can close "
            "the tab and try `/generate-highlight`.",
            ephemeral=True,
        )

    @config.command(
        name="reset",
        description="Stop using this server's custom YouTube channel; revert to the bot default.",
    )
    async def reset_(ctx: discord.ApplicationContext) -> None:
        if ctx.guild is None:
            await ctx.respond("Server only.", ephemeral=True)
            return
        if not ctx.author.guild_permissions.manage_guild:
            await ctx.respond("You need Manage Server permission.", ephemeral=True)
            return
        db.reset_guild_youtube(ctx.guild.id)
        await ctx.respond(
            "Server reverted to the bot's default YouTube channel.", ephemeral=True
        )

    bot.add_application_command(config)
