"""FFB game sound effects per pivotal play.

The FFB Java client ships its sound library under
`ffb-resources/src/main/resources/sounds/` (47 files, mostly OGG
Vorbis with a few 44.1 kHz WAV samples). Each `SoundId` maps to a
filename via `client.ini`; the in-game engine fires them on specific
events (touchdowns, casualties, blocks, kickoffs, crowd reactions).

We mirror the most evocative subset for our highlight-reel kinds:
the "what happened" SFX (kill / KO / pickup-fail / yoink) plus a
spectator-bed reaction (cheer / boo / shock / ooh) so each play has
both the on-field thud and the crowd response.

Files are fetched from FFB's GitHub raw URLs and cached on disk
under the existing cache dir alongside position icons and pitches.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

import requests

from .analyzer import PivotalPlay

log = logging.getLogger(__name__)

_FFB_RAW_BASE = (
    "https://raw.githubusercontent.com/christerk/ffb/master/"
    "ffb-resources/src/main/resources/sounds"
)
_CACHE_DIR = Path(os.environ.get(
    "FUMBBL_REPLAY_CACHE",
    str(Path.home() / ".cache" / "fumbbl-replay-video-creator")
)) / "sounds"

# Play kind -> ordered list of sound filenames. The first is the
# on-field SFX (block thud / cheer / pick-up fail), the second (when
# present) is the spectator-bed reaction. Both fire on the same beat.
_SOUND_MAP: dict[str, list[str]] = {
    "touchdown":    ["td.ogg", "specCheer.ogg"],
    # Casualty is a single kind; we differentiate by the play.detail
    # (RIP / SI / BH) at lookup time.
    "self_kill":    ["fall.ogg", "specShock.ogg"],
    "interception": ["yoink.ogg", "specCheer.ogg"],
    "double_skull": ["block.ogg", "specBoo.ogg"],
    "triple_skull": ["block.ogg", "specShock.ogg"],
    "clutch_fail":  ["pickup.ogg", "specOoh.ogg"],
}

# Casualty severity -> sounds.
_CASUALTY_SOUNDS: dict[str, list[str]] = {
    "RIP": ["rip.ogg", "specHurt.ogg"],
    "SI":  ["injury.ogg", "specHurt.ogg"],
    "BH":  ["ko.ogg", "specOoh.ogg"],
}


def sounds_for_play(play: PivotalPlay) -> list[str]:
    """Return the FFB sound filenames most appropriate for one play."""
    if play.kind == "casualty":
        return list(_CASUALTY_SOUNDS.get(play.detail.upper(), ["injury.ogg", "specHurt.ogg"]))
    return list(_SOUND_MAP.get(play.kind, []))


def fetch_sound(filename: str) -> Path | None:
    """Download a single FFB sound file by name; cache on disk.
    Returns the local path, or None if the fetch fails."""
    cache_path = _CACHE_DIR / filename
    if cache_path.exists():
        return cache_path
    url = f"{_FFB_RAW_BASE}/{filename}"
    log.info("GET %s", url)
    try:
        r = requests.get(url, timeout=30)
        r.raise_for_status()
    except Exception as e:
        log.warning("could not fetch FFB sound %s: %s", filename, e)
        return None
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_bytes(r.content)
    return cache_path


def install_play_sounds(
    plays: list[PivotalPlay],
    output_dir: Path,
) -> dict[int, list[Path]]:
    """For each pivotal play, fetch its sounds (cached) and copy them
    into `output_dir` with a sortable, descriptive name.

    Returns {play_index -> [local Paths]} so downstream tooling (mix /
    ffmpeg compose) can look up the SFX for any given play.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    out: dict[int, list[Path]] = {}
    for i, play in enumerate(plays, 1):
        files = sounds_for_play(play)
        if not files:
            continue
        copied: list[Path] = []
        for j, filename in enumerate(files):
            src = fetch_sound(filename)
            if src is None:
                continue
            # Use sortable per-play names; keep the original FFB suffix.
            dest = output_dir / f"{i:02d}_{play.kind}_{j}_{filename}"
            if dest != src:
                shutil.copyfile(src, dest)
            copied.append(dest)
        if copied:
            out[i] = copied
    return out
