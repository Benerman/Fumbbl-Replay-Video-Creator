"""Resolve a FFB replay reference (URL, .jnlp file, or raw game id) to a game id.

The FFB Java Web Start `.jnlp` descriptor is a launcher: it does not
contain the replay payload, only the arguments that tell the FFB client
which match (`-gameId`) and which websocket server (`-port`) to use.
This module pulls the `gameId` out of:

  * a FUMBBL replay URL like `https://fumbbl.com/ffblive.jnlp?replay=N`
  * a local `.jnlp` file path
  * a bare integer (treated as the game id directly)

For richer event data (turn-by-turn dice rolls etc.) you'd connect to
`ws://fumbbl.com:22223/command` and send a `clientCommandReplay` with
the gameId - see the FFB client source on GitHub. That path is not yet
implemented here.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests

log = logging.getLogger(__name__)

USER_AGENT = "fumbbl-replay-video-creator/0.1"


@dataclass
class ReplayRef:
    game_id: int
    source: str            # how we resolved it, for logging
    jnlp_args: list[str]   # full arg list when we parsed a JNLP, else []


def resolve(ref: str) -> ReplayRef:
    """Turn a user-supplied replay reference into a `ReplayRef`."""
    # 1. Bare integer
    if ref.isdigit():
        return ReplayRef(game_id=int(ref), source=f"int:{ref}", jnlp_args=[])

    # 2. URL: query string ?replay=N or /ffblive.jnlp?replay=N
    if ref.startswith(("http://", "https://")):
        qs = parse_qs(urlparse(ref).query)
        for key in ("replay", "gameId", "game", "id"):
            if key in qs and qs[key][0].isdigit():
                gid = int(qs[key][0])
                # Also fetch the JNLP so we can capture the launch args.
                args = _fetch_jnlp_args(ref)
                return ReplayRef(game_id=gid, source=f"url:{key}={gid}", jnlp_args=args)
        # Maybe the URL points at the JNLP itself but encodes id elsewhere.
        args = _fetch_jnlp_args(ref)
        gid = _game_id_from_args(args)
        if gid is not None:
            return ReplayRef(game_id=gid, source="url:jnlp-args", jnlp_args=args)
        raise ValueError(f"could not find a game id in URL: {ref}")

    # 3. Local file path
    path = Path(ref)
    if not path.exists():
        raise FileNotFoundError(ref)
    args = _parse_jnlp_file(path)
    gid = _game_id_from_args(args)
    if gid is None:
        raise ValueError(f"could not find -gameId in JNLP file: {path}")
    return ReplayRef(game_id=gid, source=f"file:{path.name}", jnlp_args=args)


def _fetch_jnlp_args(url: str) -> list[str]:
    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=20)
        r.raise_for_status()
    except requests.RequestException as e:
        log.warning("could not fetch JNLP from %s: %s", url, e)
        return []
    try:
        return _parse_jnlp_text(r.text)
    except ET.ParseError as e:
        log.warning("URL did not return valid JNLP XML: %s", e)
        return []


def _parse_jnlp_file(path: Path) -> list[str]:
    return _parse_jnlp_text(path.read_text(encoding="utf-8"))


def _parse_jnlp_text(text: str) -> list[str]:
    root = ET.fromstring(text)
    app = root.find(".//application-desc") or root.find(".//applet-desc")
    if app is None:
        return []
    return [a.text.strip() for a in app.findall("argument") if a.text]


def _game_id_from_args(args: list[str]) -> int | None:
    """Return the value following `-gameId` / `--gameId` / `-game` etc."""
    keys = {"-gameId", "--gameId", "-game", "--game", "-replay", "--replay"}
    for i, a in enumerate(args):
        if a in keys and i + 1 < len(args):
            v = args[i + 1]
            if v.isdigit():
                return int(v)
        m = re.match(r"^--?(?:gameId|game|replay)=(\d+)$", a)
        if m:
            return int(m.group(1))
    return None
