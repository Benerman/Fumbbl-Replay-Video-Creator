"""Process settings loaded from environment + .env file.

`load_settings()` returns a single `Settings` dataclass that both the
bot and the worker can pull from. The .env file is optional — when
running under docker compose env vars come straight from `env_file:`.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class Settings:
    # Discord
    discord_bot_token: str
    discord_application_id: int

    # Crypto / persistence
    fernet_master_key: str          # base64-encoded 32-byte key
    youtube_client_secrets_json: Path
    default_yt_refresh_token_json: Path | None

    # YouTube upload defaults
    youtube_privacy: str            # "public" | "unlisted" | "private"
    youtube_category_id: str

    # Rate limit
    rate_limit_per_guild_per_10min: int

    # OAuth callback (bot)
    oauth_callback_host: str        # 0.0.0.0 in docker, 127.0.0.1 locally
    oauth_callback_port: int

    @property
    def default_creds_available(self) -> bool:
        """Whether the operator's default channel has been bootstrapped."""
        return (self.default_yt_refresh_token_json is not None
                and self.default_yt_refresh_token_json.exists())


def _require(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(
            f"Required env var {name!r} is unset. See deploy/.env.example for "
            "the full list."
        )
    return val


def _optional_path(name: str) -> Path | None:
    val = os.environ.get(name)
    return Path(val) if val else None


def load_settings() -> Settings:
    """Read .env once (best-effort) then construct Settings from os.environ."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        log.debug("python-dotenv not installed; relying on raw env vars")
    else:
        # Search ./.env then ./deploy/.env so both layouts work.
        for candidate in (Path(".env"), Path("deploy/.env")):
            if candidate.exists():
                load_dotenv(candidate, override=False)
                log.debug("loaded env from %s", candidate)
                break

    return Settings(
        discord_bot_token=_require("DISCORD_BOT_TOKEN"),
        discord_application_id=int(_require("DISCORD_APPLICATION_ID")),
        fernet_master_key=_require("FERNET_MASTER_KEY"),
        youtube_client_secrets_json=Path(_require("YOUTUBE_CLIENT_SECRETS_JSON")),
        default_yt_refresh_token_json=_optional_path("DEFAULT_YT_REFRESH_TOKEN_JSON"),
        youtube_privacy=os.environ.get("YOUTUBE_PRIVACY", "public"),
        youtube_category_id=os.environ.get("YOUTUBE_CATEGORY_ID", "20"),
        rate_limit_per_guild_per_10min=int(
            os.environ.get("RATE_LIMIT_PER_GUILD_PER_10MIN", "3")
        ),
        oauth_callback_host=os.environ.get("OAUTH_CALLBACK_HOST", "127.0.0.1"),
        oauth_callback_port=int(os.environ.get("OAUTH_CALLBACK_PORT", "38080")),
    )
