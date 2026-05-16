"""Parse a FUMBBL FFB `.jnlp` launcher to get the connection parameters.

A FFB JNLP descriptor (https://fumbbl.com/ffblive.jnlp?replay=N) is a
Java Web Start launcher. It does not contain the replay payload - only
the parameters the FFB Java client needs to connect to the FFB live
server over a websocket and stream the replay.

We pull out:
  * gameId      - which replay to fetch
  * port        - websocket port (typically 22223)
  * coach       - coach name the client identifies itself with
  * codebase    - server host (e.g. fumbbl.com)
  * websocket   - derived ws://{host}:{port}/command URL

Input can be a remote URL (we fetch the JNLP) or a local file path
(we read it).
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import requests

log = logging.getLogger(__name__)

USER_AGENT = "fumbbl-replay-video-creator/0.1"


@dataclass
class JnlpReplayInfo:
    game_id: int
    server_host: str
    server_port: int
    coach: str
    main_class: str
    raw_args: list[str]

    @property
    def websocket_url(self) -> str:
        return f"ws://{self.server_host}:{self.server_port}/command"

    def as_dict(self) -> dict:
        return {
            "game_id": self.game_id,
            "server_host": self.server_host,
            "server_port": self.server_port,
            "coach": self.coach,
            "main_class": self.main_class,
            "websocket_url": self.websocket_url,
            "raw_args": self.raw_args,
        }


def load(source: str) -> JnlpReplayInfo:
    """Load a JNLP from a URL or a file path and parse it."""
    if source.startswith(("http://", "https://")):
        text = _fetch(source)
        host_hint = urlparse(source).hostname or "fumbbl.com"
    else:
        path = Path(source)
        if not path.exists():
            raise FileNotFoundError(source)
        text = path.read_text(encoding="utf-8")
        host_hint = "fumbbl.com"

    return _parse(text, fallback_host=host_hint)


def _fetch(url: str) -> str:
    log.info("GET %s", url)
    r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
    r.raise_for_status()
    return r.text


def _parse(jnlp_text: str, *, fallback_host: str) -> JnlpReplayInfo:
    root = ET.fromstring(jnlp_text)

    # codebase tells us the host the client should connect to.
    codebase = root.attrib.get("codebase") or ""
    host = urlparse(codebase).hostname or fallback_host

    app = root.find(".//application-desc") or root.find(".//applet-desc")
    if app is None:
        raise ValueError("JNLP has no <application-desc> / <applet-desc>")

    main_class = app.attrib.get("main-class", "")
    args = [a.text.strip() for a in app.findall("argument") if a.text]

    game_id = _parse_arg_int(args, "-gameId", "--gameId")
    if game_id is None:
        # FFB JNLP also uses ?replay=N in the href, fall back to that.
        href = root.attrib.get("href") or ""
        for kv in href.split("?", 1)[-1].split("&"):
            if "=" in kv:
                k, v = kv.split("=", 1)
                if k in ("replay", "gameId") and v.isdigit():
                    game_id = int(v)
                    break
    if game_id is None:
        raise ValueError("JNLP has no -gameId argument")

    port = _parse_arg_int(args, "-port", "--port") or 22223
    coach = _parse_arg_str(args, "-coach", "--coach") or "spectator"

    return JnlpReplayInfo(
        game_id=game_id,
        server_host=host,
        server_port=port,
        coach=coach,
        main_class=main_class,
        raw_args=args,
    )


def _parse_arg_int(args: list[str], *keys: str) -> int | None:
    for i, a in enumerate(args):
        if a in keys and i + 1 < len(args):
            v = args[i + 1]
            if v.isdigit():
                return int(v)
    return None


def _parse_arg_str(args: list[str], *keys: str) -> str | None:
    for i, a in enumerate(args):
        if a in keys and i + 1 < len(args):
            return args[i + 1]
    return None
