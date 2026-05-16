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
    orientation: str = "vertical",
    home_name: str | None = None,
    away_name: str | None = None,
    home_logo: Image.Image | None = None,
    away_logo: Image.Image | None = None,
    pitch_background: Image.Image | None = None,
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

    # End-frame strategy depends on the play kind.
    # - touchdown: stop in-cmd before the post-score sweep that moves
    #   every player to the dugout.
    # - casualty: walk a few commands PAST the event so the victim
    #   physically leaves the pitch (FFB fires fieldModelRemovePlayer
    #   in the same cmd as the casualty trigger). Visualises the
    #   KO/BH/SI/RIP -> dugout transition the user expects to see.
    # - other (skull, clutch_fail, interception): just the event cmd.
    if play.kind == "touchdown":
        last_stop = {"teamResultSetScore"}
    elif play.kind == "casualty":
        end = play.command_nr + 4
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

    # Pre-extract dice for EVERY command in the window (not just the
    # field-affecting ones), keyed by cmd_nr. The block roll often fires
    # in a command that doesn't change field model state (so it wouldn't
    # otherwise make `interesting_cmds`), but we still want the dice to
    # appear on the next rendered frame and linger.
    from . import dice as dice_mod
    DICE_LINGER_FRAMES = 6
    rolls_by_cmd: dict[int, list] = {}
    for c in cmds:
        cn = int(c.get("commandNr", 0) or 0)
        if cn < start or cn > end:
            continue
        groups = dice_mod.extract_for_command(replay, cn, lookback=0)
        if groups:
            rolls_by_cmd[cn] = groups

    # Render frames. Whenever a new dice group is about to appear we
    # reveal them one at a time and HOLD each new reveal for a few
    # extra frames so the viewer has time to read the block, the
    # armor, and the injury rolls separately. Field motion otherwise
    # plays at the regular `frame_ms` cadence.
    REVEAL_DWELL_FRAMES = 4   # extra frames per newly revealed dice group

    frames: list[Image.Image] = []
    durations_per_frame: list[int] = []
    active_dice: list[tuple[int, list]] = []
    prev_cn = -1

    def _render(state, dice_to_show, idx):
        img_path = out_path.with_suffix(f".frame{idx:04d}.png")
        render_tableau(
            play, state, player_lookup, img_path,
            sprites=sprites,
            orientation=orientation,
            home_name=home_name, away_name=away_name,
            home_logo=home_logo, away_logo=away_logo,
            dice=dice_to_show or None,
            pitch_background=pitch_background,
        )
        f = Image.open(img_path).convert("P", palette=Image.ADAPTIVE)
        img_path.unlink()
        return f

    frame_idx = 0
    for i, cn in enumerate(interesting_cmds):
        is_last = i == len(interesting_cmds) - 1
        state = reconstruct_at(replay, cn, stop_at=last_stop if is_last else None)

        # Collect any new dice groups that fired since the last rendered
        # cmd. We reveal them one by one so each gets its own dwell.
        new_groups: list = []
        for roll_cn in sorted(rolls_by_cmd):
            if prev_cn < roll_cn <= cn:
                new_groups.extend(rolls_by_cmd[roll_cn])

        # Age previously-active dice.
        active_dice = [(n - 1, g) for n, g in active_dice if n - 1 > 0]

        # Sequential reveal: one new group per pause, holding the field state.
        for group in new_groups:
            active_dice.append((DICE_LINGER_FRAMES, [group]))
            visible = [g for _, groups in active_dice for g in groups]
            reveal_frame = _render(state, visible, frame_idx); frame_idx += 1
            for _ in range(REVEAL_DWELL_FRAMES + 1):
                frames.append(reveal_frame)
                durations_per_frame.append(frame_ms)

        # Normal frame for the field state (no new dice).
        visible = [g for _, groups in active_dice for g in groups]
        frames.append(_render(state, visible, frame_idx)); frame_idx += 1
        durations_per_frame.append(frame_ms)

        prev_cn = cn

    # Make the final frame linger so the viewer can read the resolution.
    durations_per_frame[-1] = max(frame_ms, final_pause_ms)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    frames[0].save(
        out_path,
        save_all=True,
        append_images=frames[1:],
        duration=durations_per_frame,
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
