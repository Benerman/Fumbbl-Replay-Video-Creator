"""Discord bot entry point: `python -m services.bot`.

Starts the py-cord client, an aiohttp callback server for per-guild
OAuth flows, and a background poller that delivers job results back
to Discord.
"""

from __future__ import annotations

import asyncio
import signal

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


def main() -> None:
    ensure_dirs()
    settings = load_settings()
    get_connection()  # opens + applies schema migrations

    crypto = TokenCrypto(settings.fernet_master_key)
    in_flight = UserInFlightTracker()
    oauth = OAuthHandler(settings, crypto)

    intents = discord.Intents.default()
    bot = discord.Bot(intents=intents)

    register(bot, settings, in_flight, oauth)
    poller = Poller(bot, in_flight)

    @bot.event
    async def on_ready() -> None:  # noqa: D401
        log.info("logged in as %s (id=%s)", bot.user, bot.user and bot.user.id)
        # Lazy start of the OAuth callback server + poller after Discord is ready.
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

    # Graceful shutdown on SIGTERM (docker stop).
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def _shutdown() -> None:
        log.info("shutting down…")
        await poller.stop()
        await oauth.stop()
        await bot.close()

    def _signal(*_a) -> None:
        loop.create_task(_shutdown())

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal)
        except NotImplementedError:
            # Windows: signal handler not supported on selector loop.
            signal.signal(sig, lambda *_: _signal())

    try:
        loop.run_until_complete(bot.start(settings.discord_bot_token))
    except KeyboardInterrupt:
        loop.run_until_complete(_shutdown())
    finally:
        loop.close()


if __name__ == "__main__":
    main()
