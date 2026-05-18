"""Discord bot entry point: `python -m services.bot`.

Starts the py-cord client, an aiohttp callback server for per-guild
OAuth flows, and a background poller that delivers job results back
to Discord.

Slash-command sync notes:
  - py-cord syncs slash commands globally by default. Global commands
    can take up to an hour to propagate to all guilds. For dev /
    single-server deployments set DISCORD_DEV_GUILD_IDS in .env to a
    comma-separated list of guild ids; we'll pass them as debug_guilds
    so commands sync instantly to just those servers.
"""

from __future__ import annotations

import asyncio
import os

import discord

from services.bot.commands import register
from services.bot.oauth_handler import OAuthHandler
from services.bot.poller import Poller
from services.common.config import load_settings
from services.common.crypto import TokenCrypto
from services.common.db import get_connection, prune_rate_log
from services.common.logging import setup_logging
from services.common.paths import ensure_dirs
from services.common.rate_limit import UserInFlightTracker

log = setup_logging("services.bot")


async def _prune_loop() -> None:
    while True:
        await asyncio.sleep(300)  # 5 min
        try:
            removed = prune_rate_log()
            if removed:
                log.debug("pruned %d stale rate_log rows", removed)
        except Exception:
            log.exception("rate-log prune failed")


def _parse_debug_guilds(raw: str | None) -> list[int]:
    if not raw:
        return []
    out: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            out.append(int(part))
    return out


def main() -> None:
    """Synchronous entrypoint that hands off to py-cord's bot.run().

    bot.run() owns the event loop end-to-end — it creates the loop,
    installs SIGINT/SIGTERM handlers, runs bot.start() to completion,
    and tears the loop down cleanly. Doing it ourselves (the old
    asyncio.new_event_loop() + run_until_complete dance) was causing
    py-cord to attribute the gateway socket to a different loop than
    the one we were running on, which manifested as repeating
    "heartbeat blocked for more than 30 seconds" warnings.
    """
    ensure_dirs()
    settings = load_settings()
    get_connection()  # opens + applies schema migrations

    crypto = TokenCrypto(settings.fernet_master_key)
    in_flight = UserInFlightTracker()
    oauth = OAuthHandler(settings, crypto)

    intents = discord.Intents.default()
    debug_guilds = _parse_debug_guilds(os.environ.get("DISCORD_DEV_GUILD_IDS"))
    if debug_guilds:
        log.info("slash commands will sync INSTANTLY to guilds %s", debug_guilds)
        bot = discord.Bot(intents=intents, debug_guilds=debug_guilds)
    else:
        log.info(
            "slash commands will sync GLOBALLY (up to 1h propagation). "
            "Set DISCORD_DEV_GUILD_IDS=<guild_id,...> in .env for instant "
            "sync to specific servers."
        )
        bot = discord.Bot(intents=intents)

    register(bot, settings, in_flight, oauth)

    # Simple ping for quick connectivity testing — appears alongside
    # /generate-highlight in Discord's slash-command picker.
    @bot.slash_command(name="ping", description="Health check; replies with pong.")
    async def ping(ctx: discord.ApplicationContext) -> None:
        await ctx.respond("pong 🏓", ephemeral=True)

    poller = Poller(bot, in_flight)

    @bot.event
    async def on_ready() -> None:
        log.info("logged in as %s (id=%s)", bot.user, bot.user and bot.user.id)
        log.info(
            "%d slash command(s) registered: %s",
            len(bot.application_commands),
            ", ".join(c.name for c in bot.application_commands),
        )
        try:
            await oauth.start()
        except Exception:
            log.exception("oauth callback server failed to start")
        poller.start()
        asyncio.create_task(_prune_loop(), name="prune-loop")

    @bot.event
    async def on_application_command_error(ctx, error) -> None:
        log.exception("command error: %s", error)
        try:
            await ctx.respond(
                f"Something went wrong: `{error}`", ephemeral=True
            )
        except Exception:
            pass

    # bot.run() blocks until the gateway disconnects + signal handlers fire.
    bot.run(settings.discord_bot_token)


if __name__ == "__main__":
    main()
