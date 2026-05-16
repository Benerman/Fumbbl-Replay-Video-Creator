"""Resolve a user-supplied replay reference to FUMBBL ids.

We accept any of:

  * a bare numeric id - assumed to be a match id (e.g. `4700552`)
  * a FUMBBL replay URL: `https://fumbbl.com/ffblive.jnlp?replay=N`
    where N is the **replay id** (NOT the match id - they live in
    different id spaces, e.g. match `4700552` is replay `1901131`)
  * a FUMBBL match URL: `https://fumbbl.com/p/match?id=N`
  * a path to a local `.jnlp` file the user has saved (the
    `-gameId` argument inside the JNLP is the replay id)

Returns a `Resolved` carrying whichever ids we could derive directly.
The CLI fills in the missing one by calling the match API.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import requests

log = logging.getLogger(__name__)

USER_AGENT = "fumbbl-replay-video-creator/0.1"


@dataclass
class Resolved:
    match_id: int | None = None
    replay_id: int | None = None

    def is_empty(self) -> bool:
        return self.match_id is None and self.replay_id is None


def resolve(ref: str) -> Resolved:
    """Parse a user-supplied reference into match/replay ids."""
    ref = ref.strip()

    # Bare integer: assume match id (the matching API path is what most
    # users will reach for; we'll cross-link to replay via the summary).
    if ref.isdigit():
        return Resolved(match_id=int(ref))

    if ref.startswith(("http://", "https://")):
        url = urlparse(ref)
        qs = parse_qs(url.query)
        # FUMBBL `/p/match?id=N` carries the match id in `id`.
        if url.path.endswith("/p/match") and "id" in qs and qs["id"][0].isdigit():
            return Resolved(match_id=int(qs["id"][0]))
        # FFB JNLP carries the REPLAY id in `replay` (not the match id).
        for key in ("replay", "gameId"):
            if key in qs and qs[key] and qs[key][0].isdigit():
                return Resolved(replay_id=int(qs[key][0]))
        # Fall back to fetching and parsing the JNLP body.
        return _from_jnlp_text(_fetch(ref))

    path = Path(ref)
    if path.exists():
        return _from_jnlp_text(path.read_text(encoding="utf-8"))
    raise ValueError(f"can't resolve replay reference: {ref!r}")


def _fetch(url: str) -> str:
    log.info("GET %s", url)
    r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
    r.raise_for_status()
    return r.text


def _from_jnlp_text(text: str) -> Resolved:
    """Pull the replay id out of a JNLP descriptor.

    `-gameId N` inside the JNLP is the FFB replay id (what the live
    server's websocket uses). Treating it as a match id was the bug
    that resolved old-replay traffic to wrong matches in the same
    numeric range.
    """
    try:
        root = ET.fromstring(text)
        app = root.find(".//application-desc") or root.find(".//applet-desc")
        if app is not None:
            args = [a.text.strip() for a in app.findall("argument") if a.text]
            for i, a in enumerate(args):
                if a in ("-gameId", "--gameId") and i + 1 < len(args) and args[i + 1].isdigit():
                    return Resolved(replay_id=int(args[i + 1]))
        href = root.attrib.get("href") or ""
        qs = parse_qs(urlparse(href).query)
        for key in ("replay", "gameId"):
            if key in qs and qs[key] and qs[key][0].isdigit():
                return Resolved(replay_id=int(qs[key][0]))
    except ET.ParseError:
        pass
    m = re.search(r"(?:replay|gameId)[=\s]+(\d+)", text)
    if m:
        return Resolved(replay_id=int(m.group(1)))
    raise ValueError("could not find a gameId in JNLP content")
