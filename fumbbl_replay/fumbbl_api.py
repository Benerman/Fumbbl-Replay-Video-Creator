"""Minimal FUMBBL HTTP client.

Endpoints used:

  GET /api/match/get/{match_id}        -> match summary (teams, score, casualties)
  GET /api/team/get/{team_id}          -> full roster + player portraits
  GET /api/replay/get/{replay_id}/gz   -> gzipped JSON of the full game log

The replay endpoint streams the same per-turn event log the FFB Java
client receives over its websocket on port 22223, but as plain HTTP -
no websocket handshake, no firewalled port. The id is the `replayId`
from the match summary (it equals the match id for older games where
the field is 0).

All endpoints are unauthenticated.
"""

from __future__ import annotations

import gzip
import io
import logging
from typing import Any

import requests

log = logging.getLogger(__name__)

BASE = "https://fumbbl.com"
USER_AGENT = "fumbbl-replay-video-creator/0.1"


def fetch_match_summary(match_id: int) -> dict[str, Any]:
    return _get_json(f"{BASE}/api/match/get/{match_id}", what=f"match {match_id}")


def fetch_team(team_id: int) -> dict[str, Any]:
    return _get_json(f"{BASE}/api/team/get/{team_id}", what=f"team {team_id}")


def fetch_replay(replay_id: int) -> dict[str, Any]:
    """Fetch the full replay JSON for a given replayId.

    The endpoint serves chunked gzip. We stream and gunzip in memory.
    """
    url = f"{BASE}/api/replay/get/{replay_id}/gz"
    log.info("GET %s", url)
    r = requests.get(url, headers={"User-Agent": USER_AGENT}, stream=True, timeout=60)
    r.raise_for_status()
    buf = io.BytesIO()
    for chunk in r.iter_content(chunk_size=None):
        if chunk:
            buf.write(chunk)
    buf.seek(0)
    with gzip.GzipFile(fileobj=buf) as gz:
        import json
        data = json.load(gz)
    if not isinstance(data, dict) or "gameLog" not in data:
        raise RuntimeError(f"unexpected replay payload for replay {replay_id}")
    return data


def resolve_replay_id(match_id: int, summary: dict[str, Any] | None = None) -> int:
    """Return the replayId for a match. Falls back to match_id when 0."""
    if summary is None:
        summary = fetch_match_summary(match_id)
    rid = int(summary.get("replayId") or 0)
    return rid or match_id


def image_url(image_id: int | None) -> str | None:
    """FUMBBL serves uploaded images (team logos, player portraits) at /i/{id}."""
    if not image_id:
        return None
    return f"{BASE}/i/{image_id}"


def _get_json(url: str, *, what: str) -> dict[str, Any]:
    log.info("GET %s", url)
    r = requests.get(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}, timeout=30)
    r.raise_for_status()
    data = r.json()
    if isinstance(data, str) and data.startswith("Error"):
        raise RuntimeError(f"FUMBBL returned error for {what}: {data}")
    if not data:
        raise RuntimeError(f"empty response for {what}")
    return data
