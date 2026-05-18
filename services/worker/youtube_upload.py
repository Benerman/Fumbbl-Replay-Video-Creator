"""YouTube upload (and one-shot OAuth bootstrap).

Uses google-api-python-client's resumable upload so large MP4s
survive transient network blips. Returns the new video's ID and
public-watch URL.

Credentials live in TWO places:

  - data/youtube_token.json       — operator's default channel
    (created by `python -m services.worker.youtube_upload --bootstrap-default`)
  - guild_config.yt_refresh_token_encrypted (Fernet) for per-guild overrides

`load_credentials_for_guild(guild_id, crypto)` picks the right one.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

from services.common import db
from services.common.config import load_settings
from services.common.crypto import TokenCrypto

log = logging.getLogger(__name__)

YT_SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
              "https://www.googleapis.com/auth/youtube.readonly"]


@dataclass
class UploadResult:
    video_id: str
    url: str


def _client_secret_payload(settings) -> dict:
    return json.loads(settings.youtube_client_secrets_json.read_text())


def _credentials_from_refresh_token(settings, refresh_token: str) -> Credentials:
    sub = _client_secret_payload(settings)
    sub = sub.get("installed") or sub.get("web")
    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=sub["client_id"],
        client_secret=sub["client_secret"],
        scopes=YT_SCOPES,
    )
    creds.refresh(Request())
    return creds


def load_credentials_for_guild(guild_id: int, crypto: TokenCrypto) -> tuple[Credentials, bool]:
    """Returns (credentials, used_default_creds_flag).

    Per-guild override is consulted first; falls back to bot_defaults.
    Raises RuntimeError if neither is configured.
    """
    settings = load_settings()
    row = db.get_guild_config(guild_id)
    if row is not None and row["yt_refresh_token_encrypted"]:
        rt = crypto.decrypt(row["yt_refresh_token_encrypted"])
        return _credentials_from_refresh_token(settings, rt), False
    defaults = db.get_bot_defaults()
    if defaults is None:
        raise RuntimeError(
            "No default YouTube credentials configured. The bot operator "
            "needs to run `python -m services.worker.youtube_upload "
            "--bootstrap-default` once before any uploads can happen."
        )
    rt = crypto.decrypt(defaults["yt_refresh_token_encrypted"])
    return _credentials_from_refresh_token(settings, rt), True


def upload_video(
    creds: Credentials,
    mp4_path: Path,
    title: str,
    description: str,
    tags: list[str],
    *,
    privacy: str = "public",
    category_id: str = "20",
) -> UploadResult:
    """Resumable insert. Blocks until upload finishes (or fails)."""
    yt = build("youtube", "v3", credentials=creds, cache_discovery=False)
    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": tags,
            "categoryId": category_id,
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": False,
        },
    }
    media = MediaFileUpload(
        str(mp4_path),
        mimetype="video/mp4",
        resumable=True,
        chunksize=4 * 1024 * 1024,
    )
    req = yt.videos().insert(part="snippet,status", body=body, media_body=media)

    response = None
    last_progress_pct = -1
    while response is None:
        try:
            status, response = req.next_chunk()
        except HttpError as e:
            log.exception("YouTube HTTP error during upload: %s", e)
            raise
        if status is not None:
            pct = int(status.progress() * 100)
            if pct != last_progress_pct and pct % 10 == 0:
                log.info("upload %d%%", pct)
                last_progress_pct = pct

    video_id = response["id"]
    log.info("uploaded video id=%s", video_id)
    return UploadResult(
        video_id=video_id,
        url=f"https://www.youtube.com/watch?v={video_id}",
    )


# ---- one-shot bootstrap for the operator's default channel ----------------

def _bootstrap_default() -> int:
    """Interactive OAuth flow to mint a refresh token for the bot operator's
    default YouTube channel. Encrypts + writes it to bot_defaults.

    Designed to work both on bare-metal hosts (browser opens
    automatically) AND inside a docker container (no browser; the user
    is told to open the URL manually). The callback listener binds to
    0.0.0.0 inside the container so the host's localhost:38081 port
    forward reaches it; the redirect URI advertised to Google stays
    on "localhost" so the redirect lands on the browser's own machine.
    """
    settings = load_settings()
    crypto = TokenCrypto(settings.fernet_master_key)
    flow = InstalledAppFlow.from_client_secrets_file(
        str(settings.youtube_client_secrets_json), scopes=YT_SCOPES
    )
    bootstrap_port = settings.oauth_callback_port + 1   # avoid clashing with bot
    print(
        f"\nOpen this URL in your browser to authorize YouTube uploads:\n"
        f"  → (URL will print below once the local listener is up)\n"
        f"\nIf this is running inside docker, make sure the host port "
        f"{bootstrap_port} is forwarded to the container — it is by "
        f"default in deploy/docker-compose.yml.\n"
    )
    try:
        creds = flow.run_local_server(
            host="localhost",            # what we advertise as the redirect URI
            bind_addr="0.0.0.0",         # what we actually listen on
            port=bootstrap_port,
            open_browser=False,          # no browser in containers; the user opens manually
            authorization_prompt_message="Please open this URL in your browser to authorize:\n  {url}",
            success_message=(
                "Authorization complete. You can close this tab and "
                "return to the terminal."
            ),
        )
    except OSError as e:
        print(
            f"\nERROR: could not bind to port {bootstrap_port}. "
            "Stop anything else using it (lsof -i :"
            f"{bootstrap_port}) or set OAUTH_CALLBACK_PORT in your "
            ".env to choose a different base port.\n"
            f"Underlying error: {e}"
        )
        return 1
    if not creds.refresh_token:
        print("ERROR: Google did not return a refresh token. Try "
              "revoking the bot's access at "
              "https://myaccount.google.com/permissions and re-running.")
        return 1
    # Look up the channel id we just authorized for.
    yt = build("youtube", "v3", credentials=creds, cache_discovery=False)
    try:
        channels = yt.channels().list(part="id", mine=True).execute()
        ch_id = (channels.get("items") or [{}])[0].get("id")
    except Exception:
        ch_id = None
    encrypted = crypto.encrypt(creds.refresh_token)
    db.set_bot_defaults(refresh_token_encrypted=encrypted, yt_channel_id=ch_id)
    print(f"Saved default credentials. Channel id: {ch_id}")
    return 0


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="YouTube upload helpers.")
    parser.add_argument("--bootstrap-default", action="store_true",
                        help="Run the one-shot OAuth flow for the bot operator's default channel.")
    args = parser.parse_args(argv)
    if args.bootstrap_default:
        return _bootstrap_default()
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(_main())
