"""Backfill the processed_replays table from a guild's YouTube channel.

If somebody nukes data/app.sqlite3 we want to avoid re-uploading
every replay on the next request. This module scans the configured
guild's "uploads" playlist, parses `Match {id}` out of titles, and
inserts matching rows into processed_replays.

Conservative by design: parsing fails open. Any video without a
`Match {id}` title or without a parseable id is skipped.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Iterable

from googleapiclient.discovery import build

from services.common import db

log = logging.getLogger(__name__)

_TITLE_MATCH_ID = re.compile(r"Match\s+(\d{4,12})")


@dataclass
class ReconcileStats:
    scanned: int = 0
    matched: int = 0
    written: int = 0


def reconcile(creds, guild_id: int, used_default_creds: bool,
              max_pages: int = 10) -> ReconcileStats:
    yt = build("youtube", "v3", credentials=creds, cache_discovery=False)
    # 1. Find the authenticated user's "uploads" playlist id.
    channels = yt.channels().list(part="contentDetails", mine=True).execute()
    items = channels.get("items") or []
    if not items:
        log.warning("no channel for these credentials; nothing to reconcile")
        return ReconcileStats()
    uploads_pl = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]
    # 2. Walk the uploads playlist.
    stats = ReconcileStats()
    page_token = None
    pages = 0
    while pages < max_pages:
        resp = yt.playlistItems().list(
            part="snippet",
            playlistId=uploads_pl,
            maxResults=50,
            pageToken=page_token,
        ).execute()
        for item in resp.get("items") or []:
            stats.scanned += 1
            snippet = item.get("snippet") or {}
            title = snippet.get("title", "")
            m = _TITLE_MATCH_ID.search(title)
            if not m:
                continue
            match_id = int(m.group(1))
            video_id = snippet.get("resourceId", {}).get("videoId")
            if not video_id:
                continue
            stats.matched += 1
            existing = db.find_processed(guild_id, match_id, None)
            if existing is not None:
                continue
            db.record_processed(
                guild_id=guild_id,
                match_id=match_id,
                replay_id=None,
                youtube_video_id=video_id,
                youtube_url=f"https://www.youtube.com/watch?v={video_id}",
                used_default_creds=used_default_creds,
            )
            stats.written += 1
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
        pages += 1
    log.info("reconcile guild=%s scanned=%d matched=%d written=%d",
             guild_id, stats.scanned, stats.matched, stats.written)
    return stats
