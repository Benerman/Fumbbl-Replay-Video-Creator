"""Tiny aiohttp callback server for the per-guild YouTube OAuth flow.

When an admin runs `/highlight-config set-youtube`, the bot:
  1. Mints a one-time `state` value (UUID) and stores `(state -> guild_id)`
     in a 10-minute in-memory cache.
  2. Replies ephemerally with the Google auth URL.
  3. Waits for the user to authorize in their browser; Google redirects
     back to our callback with `?code=…&state=…`.
  4. Callback exchanges the code for a refresh token, encrypts it,
     and writes the guild_config row.

This server only ever runs alongside the bot process (same event loop).
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from services.common.config import Settings
    from services.common.crypto import TokenCrypto

log = logging.getLogger(__name__)

YT_SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
              "https://www.googleapis.com/auth/youtube.readonly"]
STATE_TTL_SECONDS = 10 * 60


@dataclass
class _Pending:
    guild_id: int
    user_id: int
    created_at: datetime = field(default_factory=datetime.utcnow)

    def expired(self) -> bool:
        return datetime.utcnow() - self.created_at > timedelta(seconds=STATE_TTL_SECONDS)


class OAuthHandler:
    """Single instance owned by the bot. Holds state map + server."""

    def __init__(self, settings: "Settings", crypto: "TokenCrypto") -> None:
        self._settings = settings
        self._crypto = crypto
        self._pending: dict[str, _Pending] = {}
        self._runner: web.AppRunner | None = None
        self._lock = asyncio.Lock()

    def begin(self, guild_id: int, user_id: int) -> tuple[str, str]:
        """Returns (state, auth_url) for a fresh OAuth flow."""
        state = secrets.token_urlsafe(24)
        self._pending[state] = _Pending(guild_id=guild_id, user_id=user_id)
        auth_url = self._build_auth_url(state)
        log.info("started oauth flow guild=%s state=%s", guild_id, state[:8])
        return state, auth_url

    def _build_auth_url(self, state: str) -> str:
        # Built locally so we don't have to spin a Flow per call.
        import json
        import urllib.parse
        secrets_path = self._settings.youtube_client_secrets_json
        try:
            data = json.loads(secrets_path.read_text())
        except FileNotFoundError:
            raise RuntimeError(
                f"YouTube client secrets file not found at {secrets_path}. "
                "Set YOUTUBE_CLIENT_SECRETS_JSON to a downloaded OAuth client."
            )
        client_id = data["installed"]["client_id"] if "installed" in data else data["web"]["client_id"]
        params = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": self._redirect_uri(),
            "scope": " ".join(YT_SCOPES),
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
        }
        return "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(params)

    def _redirect_uri(self) -> str:
        # MUST use the redirect_host (localhost/127.0.0.1), not the
        # bind host. Google rejects 0.0.0.0 as a redirect URI even for
        # Desktop OAuth clients — error 400 invalid_request.
        return (
            f"http://{self._settings.oauth_redirect_host}:"
            f"{self._settings.oauth_callback_port}/oauth/callback"
        )

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/oauth/callback", self._handle_callback)
        app.router.add_get("/healthz", lambda _: web.Response(text="ok"))
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(
            self._runner,
            self._settings.oauth_callback_host,
            self._settings.oauth_callback_port,
        )
        await site.start()
        log.info("oauth callback listening on %s", self._redirect_uri())

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()

    async def _handle_callback(self, request: web.Request) -> web.Response:
        state = request.query.get("state", "")
        code = request.query.get("code", "")
        async with self._lock:
            pending = self._pending.pop(state, None)
        if pending is None or pending.expired() or not code:
            return web.Response(
                status=400,
                text="OAuth state expired or unknown. Please re-run /highlight-config set-youtube.",
            )
        # Exchange code -> refresh token via Google's token endpoint.
        try:
            refresh_token, channel_id = await self._exchange_code(code)
        except Exception as e:
            log.exception("token exchange failed for guild=%s", pending.guild_id)
            return web.Response(status=500, text=f"Token exchange failed: {e}")

        # Persist (encrypted) under the requesting guild.
        from services.common import db
        encrypted = self._crypto.encrypt(refresh_token)
        db.set_guild_youtube(
            guild_id=pending.guild_id,
            refresh_token_encrypted=encrypted,
            yt_channel_id=channel_id,
            set_by_user_id=pending.user_id,
        )
        log.info("stored encrypted YT creds for guild=%s channel=%s",
                 pending.guild_id, channel_id)
        return web.Response(
            text="YouTube channel linked successfully. You can close this tab "
                  "and return to Discord.",
            content_type="text/plain",
        )

    async def _exchange_code(self, code: str) -> tuple[str, str | None]:
        """Code -> refresh_token + channel_id."""
        import json
        import aiohttp
        data = json.loads(self._settings.youtube_client_secrets_json.read_text())
        sub = data.get("installed") or data.get("web")
        client_id = sub["client_id"]
        client_secret = sub["client_secret"]
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "code": code,
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "redirect_uri": self._redirect_uri(),
                    "grant_type": "authorization_code",
                },
            ) as resp:
                token_payload = await resp.json()
            if "refresh_token" not in token_payload:
                raise RuntimeError(
                    f"No refresh_token in Google's response: {token_payload}"
                )
            access_token = token_payload["access_token"]
            refresh_token = token_payload["refresh_token"]

            # Look up the user's YT channel id so we can show it later.
            async with session.get(
                "https://www.googleapis.com/youtube/v3/channels"
                "?part=id&mine=true",
                headers={"Authorization": f"Bearer {access_token}"},
            ) as resp:
                channels = await resp.json()
        channel_id = None
        if channels.get("items"):
            channel_id = channels["items"][0]["id"]
        return refresh_token, channel_id
