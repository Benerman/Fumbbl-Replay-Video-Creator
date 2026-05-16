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

# Each play resolves to (on-field SFX, crowd-bed SFX). We pick the
# crowd bed from a SET of candidates and rotate by play index so a
# match doesn't sound like one looped reaction.

# Crowd beds by mood. specStomp lands hard on fouls and big hits;
# specCheer / specClap on positive scoring beats; specHurt / specOoh
# on injuries; specBoo / specLaugh / specShock on blunders.
_CROWD_TD_DEFAULT       = ["specCheer.ogg", "specClap.ogg", "specAah.ogg"]
_CROWD_TD_DECISIVE      = ["specStomp.ogg", "specCheer.ogg"]            # game-winning / tying
_CROWD_TD_QUIET         = ["specAah.ogg", "specClap.ogg"]                # mop-up scores
_CROWD_KILL             = ["specStomp.ogg", "specHurt.ogg", "specShock.ogg"]
_CROWD_KILL_FOUL        = ["specBoo.ogg", "specStomp.ogg"]               # boo the foul, stomp louder
_CROWD_SI               = ["specHurt.ogg", "specOoh.ogg", "specStomp.ogg"]
_CROWD_SI_FOUL          = ["specBoo.ogg", "specStomp.ogg"]
_CROWD_BH               = ["specOoh.ogg", "specClap.ogg", "specStomp.ogg"]
_CROWD_BH_FOUL          = ["specBoo.ogg", "specStomp.ogg"]
_CROWD_CROWD_PUSH       = ["specStomp.ogg", "specCheer.ogg"]             # the crowd literally pushes
_CROWD_BLUNDER          = ["specBoo.ogg", "specLaugh.ogg", "specShock.ogg"]
_CROWD_TRIPLE_SKULL     = ["specShock.ogg", "specLaugh.ogg"]
_CROWD_CLUTCH_NO_WIN    = ["specShock.ogg", "specBoo.ogg"]
_CROWD_CLUTCH_REG       = ["specOoh.ogg", "specBoo.ogg"]
_CROWD_SELF_KILL        = ["specLaugh.ogg", "specShock.ogg"]
_CROWD_INTERCEPTION     = ["specCheer.ogg", "specStomp.ogg"]


def sounds_for_play(play: PivotalPlay, *, play_index: int = 0) -> list[str]:
    """Return the FFB sound filenames most appropriate for one play.

    play_index seeds a rotation across the candidate crowd beds so
    consecutive plays of the same kind get different fan reactions.
    """
    on_field, crowd_pool = _resolve_sounds(play)
    if not on_field and not crowd_pool:
        return []
    crowd = crowd_pool[play_index % len(crowd_pool)] if crowd_pool else None
    return [s for s in (on_field, crowd) if s]


def _resolve_sounds(play: PivotalPlay) -> tuple[str | None, list[str]]:
    """Map (kind, detail, reason, tags) to (on-field SFX, crowd pool)."""
    tags = play.tags or []
    if play.kind == "touchdown":
        if "game_winning" in tags or "tying" in tags:
            return "td.ogg", _CROWD_TD_DECISIVE
        if "comeback" in tags:
            return "td.ogg", _CROWD_TD_DEFAULT
        return "td.ogg", _CROWD_TD_QUIET if play.score_home is not None and abs(
            (play.score_home or 0) - (play.score_away or 0)) >= 2 else _CROWD_TD_DEFAULT
    if play.kind == "casualty":
        detail = (play.detail or "").upper()
        reason = (play.reason or "").lower()
        if reason == "crowdpushed":
            on = {"RIP": "rip.ogg", "SI": "injury.ogg"}.get(detail, "ko.ogg")
            return on, _CROWD_CROWD_PUSH
        if detail == "RIP":
            return "rip.ogg", _CROWD_KILL_FOUL if reason == "fouled" else _CROWD_KILL
        if detail == "SI":
            return "injury.ogg", _CROWD_SI_FOUL if reason == "fouled" else _CROWD_SI
        if detail == "BH":
            return "ko.ogg", _CROWD_BH_FOUL if reason == "fouled" else _CROWD_BH
    if play.kind == "self_kill":
        return "fall.ogg", _CROWD_SELF_KILL
    if play.kind == "interception":
        return "yoink.ogg", _CROWD_INTERCEPTION
    if play.kind == "triple_skull":
        return "block.ogg", _CROWD_TRIPLE_SKULL
    if play.kind == "double_skull":
        return "block.ogg", _CROWD_BLUNDER
    if play.kind == "clutch_fail":
        return "pickup.ogg", _CROWD_CLUTCH_NO_WIN if "no_win" in tags else _CROWD_CLUTCH_REG
    return None, []


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


def resolve_play_sounds(plays: list[PivotalPlay]) -> dict[int, list[Path]]:
    """For each pivotal play return the cached on-disk paths for its
    SFX. The files keep their FFB names (`td.ogg`, `specStomp.ogg`,
    ...) and live in the shared cache root - one copy per filename,
    referenced by every match that needs them. Downloads on first
    use; subsequent runs serve from cache.

    Returns {play_index -> [Paths]} pointing into the cache.
    """
    out: dict[int, list[Path]] = {}
    for i, play in enumerate(plays, 1):
        files = sounds_for_play(play, play_index=i)
        if not files:
            continue
        paths = [p for p in (fetch_sound(f) for f in files) if p]
        if paths:
            out[i] = paths
    return out


def install_play_sounds(
    plays: list[PivotalPlay],
    output_dir: Path | None = None,
) -> dict[int, list[Path]]:
    """Optional helper: COPY the resolved SFX into a per-match
    directory under their original FFB filenames (so a user who
    wants a self-contained match output bundle can have one).
    Without `output_dir` this is equivalent to resolve_play_sounds.
    """
    resolved = resolve_play_sounds(plays)
    if output_dir is None:
        return resolved
    output_dir.mkdir(parents=True, exist_ok=True)
    out: dict[int, list[Path]] = {}
    seen: set[str] = set()
    for idx, paths in resolved.items():
        copied: list[Path] = []
        for src in paths:
            dest = output_dir / src.name
            if src.name not in seen:
                shutil.copyfile(src, dest)
                seen.add(src.name)
            copied.append(dest)
        out[idx] = copied
    return out
