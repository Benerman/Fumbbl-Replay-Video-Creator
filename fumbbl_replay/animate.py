"""Render an animated GIF of the replay around a pivotal play.

We walk the gameLog from a start command up to (and including) the
target command, snapshot the field state after every command that
moved a player or the ball, render each as a tableau frame, then
combine the frames into an animated GIF.

The result is a short clip of the scoring drive (or the run-up to
a casualty) - useful for previewing pivotal plays without having to
fire up the FFB Java client.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from PIL import Image

from .analyzer import PivotalPlay
from .events import PlayerInfo
from .field_state import FieldState, reconstruct_at
from .tableau import render_tableau

# Field-affecting modelChangeIds. A frame is worth rendering only when
# one of these fired in the command - dice, dialog, and turn-counter
# changes don't visibly move anything.
_FIELD_AFFECTING = {
    "fieldModelSetPlayerCoordinate",
    "fieldModelRemovePlayer",
    "fieldModelSetBallCoordinate",
    "fieldModelSetBallInPlay",
    "fieldModelSetPlayerState",
}


def render_play_gif(
    replay: dict[str, Any],
    play: PivotalPlay,
    player_lookup: dict[str, PlayerInfo],
    out_path: Path,
    *,
    sprites: dict[str, Image.Image] | None = None,
    home_name: str | None = None,
    away_name: str | None = None,
    home_logo: Image.Image | None = None,
    away_logo: Image.Image | None = None,
    lookback_cmds: int = 60,
    frame_ms: int = 200,
    final_pause_ms: int = 1500,
    max_frames: int = 50,
) -> Path:
    """Render an animated GIF of the play's run-up.

    `lookback_cmds` is how far back from the play's command_nr we
    start. Defaults to 60 - typically covers the whole scoring drive
    or the action sequence leading to a casualty.
    """
    if play.command_nr is None:
        raise ValueError("play has no command_nr; cannot animate")

    end = play.command_nr
    start = max(1, end - lookback_cmds)
    cmds: Iterable[dict[str, Any]] = replay.get("gameLog", {}).get("commandArray", []) or []

    # The end-frame stop_at is the same as for static tableaux.
    if play.kind == "touchdown":
        last_stop = {"teamResultSetScore"}
    elif play.kind == "casualty":
        # For casualties we already snapshot cmd_nr - 1 in the static
        # path; for the animation the final frame is just cmd_nr - 1
        # too, so no in-cmd stop is needed.
        end = end - 1
        last_stop = set()
    else:
        last_stop = set()

    interesting_cmds: list[int] = []
    for c in cmds:
        cn = int(c.get("commandNr", 0) or 0)
        if cn < start or cn > end:
            continue
        if c.get("netCommandId") != "serverModelSync":
            continue
        for m in c.get("modelChangeList", {}).get("modelChangeArray", []) or []:
            if m.get("modelChangeId") in _FIELD_AFFECTING:
                interesting_cmds.append(cn)
                break

    if not interesting_cmds:
        # Fall back to a single frame of the static tableau.
        interesting_cmds = [end]

    # Trim to max_frames, keeping the last frames (closest to the play).
    if len(interesting_cmds) > max_frames:
        interesting_cmds = interesting_cmds[-max_frames:]

    # Render frames. The final frame uses the play's static stop_at.
    frames: list[Image.Image] = []
    for i, cn in enumerate(interesting_cmds):
        is_last = i == len(interesting_cmds) - 1
        state = reconstruct_at(replay, cn, stop_at=last_stop if is_last else None)
        img_path = out_path.with_suffix(f".frame{i:03d}.png")
        render_tableau(
            play, state, player_lookup, img_path,
            sprites=sprites,
            home_name=home_name, away_name=away_name,
            home_logo=home_logo, away_logo=away_logo,
        )
        frames.append(Image.open(img_path).convert("P", palette=Image.ADAPTIVE))
        img_path.unlink()  # keep only the GIF

    durations = [frame_ms] * len(frames)
    durations[-1] = max(frame_ms, final_pause_ms)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    frames[0].save(
        out_path,
        save_all=True,
        append_images=frames[1:],
        duration=durations,
        loop=0,
        optimize=False,
        disposal=2,
    )
    return out_path


def render_field_state_frames(
    replay: dict[str, Any],
    cmd_range: tuple[int, int],
    player_lookup: dict[str, PlayerInfo],
) -> list[FieldState]:
    """Helper: just snapshot states for every interesting command in the range."""
    start, end = cmd_range
    states: list[FieldState] = []
    cmds = replay.get("gameLog", {}).get("commandArray", []) or []
    for c in cmds:
        cn = int(c.get("commandNr", 0) or 0)
        if cn < start or cn > end:
            continue
        if c.get("netCommandId") != "serverModelSync":
            continue
        if any(m.get("modelChangeId") in _FIELD_AFFECTING
               for m in c.get("modelChangeList", {}).get("modelChangeArray", []) or []):
            states.append(reconstruct_at(replay, cn))
    return states
