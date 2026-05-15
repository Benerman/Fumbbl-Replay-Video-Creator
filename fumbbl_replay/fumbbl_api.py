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


def fetch_match_summary(game_id: int) -> dict[str, Any]:
    url = f"{BASE}/api/match/get/{game_id}"
    log.info("GET %s", url)
    r = requests.get(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}, timeout=30)
    r.raise_for_status()
    data = r.json()
    if isinstance(data, str) and data.startswith("Error"):
        raise RuntimeError(f"FUMBBL returned error: {data}")
    if not data or "team1" not in data:
        raise RuntimeError(f"no match summary returned for {game_id}: {data!r}")
    return data
