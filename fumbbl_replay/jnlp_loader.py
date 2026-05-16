"""Resolve a user-supplied replay reference to a numeric match id.

We accept any of:

  * a bare numeric id (e.g. `1901135`)
  * a FUMBBL replay URL (e.g. `https://fumbbl.com/ffblive.jnlp?replay=1901135`)
  * a path to a local `.jnlp` file the user has saved

JNLP descriptors are Java Web Start launchers used by the official FFB
client. We don't need the launcher itself - we just want the gameId.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import requests

log = logging.getLogger(__name__)

USER_AGENT = "fumbbl-replay-video-creator/0.1"


def resolve(ref: str) -> int:
    """Return the numeric match id for a replay reference (URL, file, or bare id)."""
    ref = ref.strip()
    if ref.isdigit():
        return int(ref)
    if ref.startswith(("http://", "https://")):
        # Many FFB links carry ?replay=N in the query string already; check that first.
        qs = parse_qs(urlparse(ref).query)
        for key in ("replay", "gameId"):
            if key in qs and qs[key] and qs[key][0].isdigit():
                return int(qs[key][0])
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


def _from_jnlp_text(text: str) -> int:
    # Try strict XML first; FUMBBL's JNLP is well-formed.
    try:
        root = ET.fromstring(text)
        app = root.find(".//application-desc") or root.find(".//applet-desc")
        if app is not None:
            args = [a.text.strip() for a in app.findall("argument") if a.text]
            for i, a in enumerate(args):
                if a in ("-gameId", "--gameId") and i + 1 < len(args) and args[i + 1].isdigit():
                    return int(args[i + 1])
        # Fall through: look for ?replay=N in <jnlp href="...">
        href = root.attrib.get("href") or ""
        qs = parse_qs(urlparse(href).query)
        for key in ("replay", "gameId"):
            if key in qs and qs[key] and qs[key][0].isdigit():
                return int(qs[key][0])
    except ET.ParseError:
        pass
    # Last resort: regex sweep.
    m = re.search(r"(?:replay|gameId)[=\s]+(\d+)", text)
    if m:
        return int(m.group(1))
    raise ValueError("could not find a gameId in JNLP content")
