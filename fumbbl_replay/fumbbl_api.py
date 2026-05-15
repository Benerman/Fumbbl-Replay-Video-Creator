"""Minimal FUMBBL HTTP client.

Right now we only need the match summary endpoint:

  GET https://fumbbl.com/api/match/get/{game_id}
      -> JSON: teams, score, casualties, division, coaches, gate, etc.

The endpoint takes no auth. Full event-log (per-turn dice and actions)
is NOT exposed via plain HTTP - the FFB Java client streams it over a
websocket on port 22223. See jnlp_loader.py for notes on that path.
"""

from __future__ import annotations

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
