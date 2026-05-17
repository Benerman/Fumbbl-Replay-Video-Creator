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

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from PIL import Image

from .analyzer import PivotalPlay
from .events import PlayerInfo
from .field_state import FieldState, reconstruct_at
from .tableau import render_tableau


@dataclass
class GifResult:
    """Output of render_play_gif. impact_ms is how many milliseconds into
    the clip the key visual moment (last dice reveal, or second-to-last
    frame for dice-less plays) lands — the mixer uses this to align the
    SFX bed with what the viewer is actually seeing.

    When frames_dir is set, the renderer also writes a full-resolution,
    full-colour PNG sequence into that directory along with a `concat.txt`
    ffmpeg can read directly. compose.py prefers this over the lossy
    GIF intermediate so the final video keeps its colour depth."""
    path: Path
    impact_ms: int
    total_ms: int
    frames_dir: Path | None = None

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
    weather: str | None = None,
    lookback_cmds: int = 60,
    gif_scale: float = 0.55,           # output GIF dimensions vs. tableau (smaller = faster + smaller files)
    palette_colors: int = 96,          # fewer colours = smaller GIF, blockier gradients
    frame_ms: int = 200,
    final_pause_ms: int = 1500,
    max_frames: int = 50,
    frames_dir: Path | None = None,    # if set, also dump full-res PNG sequence + concat.txt here
) -> GifResult:
    """Render an animated GIF of the play's run-up.

    `lookback_cmds` is how far back from the play's command_nr we
    start. Defaults to 60 - typically covers the whole scoring drive
    or the action sequence leading to a casualty.

    Returns a GifResult carrying the gif path, the impact_ms timestamp
    (when the climactic moment lands in the clip — used by the audio
    mixer to align SFX with the visual), and total_ms.
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
    # - double/triple skull: walk a few commands past so the attacker's
    #   own armor + injury rolls (he fell on his face) are included.
    # - other (clutch_fail, interception): just the event cmd.
    if play.kind == "touchdown":
        last_stop = {"teamResultSetScore"}
    elif play.kind == "casualty":
        end = play.command_nr + 4
        last_stop = set()
    elif play.kind in ("double_skull", "triple_skull"):
        end = play.command_nr + 6
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

    # Pre-extract dice for the play. We scope dice to the cmds where
    # the play's actor was on the clock — that way a blitz block fired
    # 18 cmds before the TD (well outside a fixed-window lookback)
    # still gets captured for its own scorer, while a teammate's
    # earlier block roll in the same drive gets filtered out.
    #
    # For casualties the "actor" is the inflicter (the one rolling
    # dice against the victim). For everything else it's play.player_id.
    from . import dice as dice_mod
    DICE_LINGER_FRAMES = 6
    WIDE_DICE_LOOKBACK = 30
    actor_pid = play.inflicter_id if play.kind == "casualty" else play.player_id
    # Pre-walk to figure out per-cmd whether the crosshair should be
    # showing on this play's actor's blitz target. Trigger is the
    # selectBlitzTarget REPORT — that fires the moment the FFB Java
    # client visually places the cursor on the chosen opponent. The
    # badge stays visible until the actor's turn ends (i.e. until
    # actingPlayerSetPlayerId clears them off the clock).
    acting_at_cmd: dict[int, str | None] = {}
    blitz_active_at_cmd: dict[int, bool] = {}
    acting_now: str | None = None
    blitz_target_selected: bool = False
    for c in cmds:
        cn = int(c.get("commandNr", 0) or 0)
        if cn > end:
            break
        if c.get("netCommandId") != "serverModelSync":
            continue
        for m in c.get("modelChangeList", {}).get("modelChangeArray", []) or []:
            mid = m.get("modelChangeId")
            v = m.get("modelChangeValue")
            if mid == "actingPlayerSetPlayerId":
                new_pid = str(v) if v else None
                # When the acting player actually changes we drop the
                # crosshair — any prior selection belongs to a finished
                # action. Same-pid refires (FFB emits these for sub-
                # steps of one action) leave the state intact.
                if new_pid != acting_now:
                    blitz_target_selected = False
                acting_now = new_pid
        for r in (c.get("reportList") or {}).get("reports") or []:
            if r.get("reportId") != "selectBlitzTarget":
                continue
            attacker = str(r.get("attackerId") or "")
            defender = str(r.get("defenderId") or "")
            if (actor_pid and attacker == actor_pid
                    and play.blitz_target_id
                    and defender == play.blitz_target_id):
                blitz_target_selected = True
        acting_at_cmd[cn] = acting_now
        blitz_active_at_cmd[cn] = (
            blitz_target_selected
            and acting_now is not None
            and actor_pid is not None
            and acting_now == actor_pid
        )
    dice_start = max(start, play.command_nr - WIDE_DICE_LOOKBACK)
    # Snake-eyes (double_skull / triple_skull) sends the attacker
    # face-first into the turf — the armor and injury rolls for HIS
    # going-down fire in the cmds AFTER blockRoll, so we extend the
    # window past play.command_nr to include those. Other kinds end
    # on the trigger cmd, so dice_end stays at play.command_nr.
    POST_IMPACT_TAIL = 4
    if play.kind in ("double_skull", "triple_skull"):
        dice_end = play.command_nr + POST_IMPACT_TAIL
    else:
        dice_end = play.command_nr
    rolls_by_cmd: dict[int, list] = {}
    for c in cmds:
        cn = int(c.get("commandNr", 0) or 0)
        if cn < dice_start or cn > dice_end:
            continue
        # Only include dice rolled while the play's own actor was on
        # the clock — keeps teammates' earlier dice from leaking in.
        # For the post-impact tail (armor/injury after a snake-eyes
        # going-down), FFB has already cleared acting_player so we
        # bypass the filter for those cmds.
        if actor_pid and cn <= play.command_nr and acting_at_cmd.get(cn) != actor_pid:
            continue
        groups = dice_mod.extract_for_command(replay, cn, lookback=0)
        if groups:
            rolls_by_cmd[cn] = groups

    # Render frames. Whenever a new dice group is about to appear we
    # reveal them one at a time and HOLD each new reveal for a generous
    # beat so the viewer can read the block / armor / injury rolls.
    # Combined with the 2x movement speed-up, this gives the action
    # moment good emphasis against the fast travel before and after.
    REVEAL_DWELL_FRAMES = 8   # extra frames per newly revealed dice group

    frames: list[Image.Image] = []
    durations_per_frame: list[int] = []
    frame_pngs: list[Path | None] = []      # parallel to frames[]: full-res PNG path or None
    # True when this frame has no dice showing (pure movement). Post-loop
    # we halve durations on runs of >= MOVEMENT_SPEEDUP_THRESHOLD pure
    # movement frames so long walks-to-the-endzone don't drag.
    movement_only: list[bool] = []
    active_dice: list[tuple[int, list]] = []
    prev_cn = -1
    if frames_dir is not None:
        frames_dir.mkdir(parents=True, exist_ok=True)

    def _render(state, dice_to_show, idx, blitz_active=False):
        """Render one tableau. Returns (palette_image_for_gif, full_res_png_path_or_None)."""
        img_path = out_path.with_suffix(f".frame{idx:04d}.png")
        render_tableau(
            play, state, player_lookup, img_path,
            sprites=sprites,
            orientation=orientation,
            home_name=home_name, away_name=away_name,
            home_logo=home_logo, away_logo=away_logo,
            dice=dice_to_show or None,
            pitch_background=pitch_background,
            weather=weather,
            blitz_active=blitz_active,
        )
        im = Image.open(img_path)
        if gif_scale != 1.0:
            sw, sh = im.size
            im_small = im.resize((int(sw * gif_scale), int(sh * gif_scale)),
                                  resample=Image.LANCZOS)
        else:
            im_small = im
        f = im_small.convert("P", palette=Image.ADAPTIVE, colors=palette_colors)
        # Preserve the full-res PNG for video encoding when requested;
        # otherwise drop it to save disk.
        if frames_dir is not None:
            dst = frames_dir / f"frame_{idx:04d}.png"
            img_path.replace(dst)
            return f, dst
        img_path.unlink()
        return f, None

    frame_idx = 0
    # Track the LAST appended dice-reveal frame so the audio mix
    # can hit SFX at the exact moment the dice land in the clip.
    last_dice_frame_idx: int | None = None
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

        blitz_active_now = blitz_active_at_cmd.get(cn, False)

        # Sequential reveal: one new group per pause, holding the field state.
        for group in new_groups:
            active_dice.append((DICE_LINGER_FRAMES, [group]))
            visible = [g for _, groups in active_dice for g in groups]
            reveal_frame, reveal_png = _render(state, visible, frame_idx, blitz_active=blitz_active_now); frame_idx += 1
            for _ in range(REVEAL_DWELL_FRAMES + 1):
                frames.append(reveal_frame)
                durations_per_frame.append(frame_ms)
                frame_pngs.append(reveal_png)
                movement_only.append(False)
            last_dice_frame_idx = len(frames) - 1

        # Normal frame for the field state (no new dice).
        visible = [g for _, groups in active_dice for g in groups]
        frame_img, frame_png = _render(state, visible, frame_idx, blitz_active=blitz_active_now); frame_idx += 1
        frames.append(frame_img)
        durations_per_frame.append(frame_ms)
        frame_pngs.append(frame_png)
        # "Movement only" = no dice fired this cmd AND no dice still on
        # screen from earlier. These frames are eligible for the
        # speed-up pass below.
        movement_only.append(not new_groups and not active_dice)

        prev_cn = cn

    # Speed-up pass: when 5+ consecutive frames are pure movement, cap
    # their durations so long walks-to-the-endzone don't drag. Runs
    # that cross into a dice section snap back to normal speed — the
    # action moment lands at the regular cadence.
    MOVEMENT_SPEEDUP_THRESHOLD = 5
    FAST_FRAME_MS = 140             # was 100 (full 2x speed-up); 140 is ~30% slower
    i = 0
    while i < len(movement_only):
        if movement_only[i]:
            run_start = i
            while i < len(movement_only) and movement_only[i]:
                i += 1
            if i - run_start >= MOVEMENT_SPEEDUP_THRESHOLD:
                for k in range(run_start, i):
                    durations_per_frame[k] = min(durations_per_frame[k], FAST_FRAME_MS)
        else:
            i += 1

    # Make the final frame linger so the viewer can read the resolution.
    # Has to run AFTER the speed-up pass — otherwise we'd halve the
    # resolution hold along with the movement.
    durations_per_frame[-1] = max(frame_ms, final_pause_ms)

    # Impact = first frame of the dice-reveal hold for the LAST dice
    # group; for dice-less plays (most pure-movement TDs), use the
    # frame just before the lingering resolution frame. We aim 1 frame
    # back from the end of the dice hold so the SFX hits as the dice
    # settle, not after.
    if last_dice_frame_idx is not None:
        impact_frame_idx = max(0, last_dice_frame_idx - REVEAL_DWELL_FRAMES)
    else:
        impact_frame_idx = max(0, len(frames) - 2)
    impact_ms = sum(durations_per_frame[:impact_frame_idx + 1])
    total_ms = sum(durations_per_frame)

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

    # Emit an ffmpeg concat list alongside the PNGs. We group consecutive
    # references to the same frame (e.g. a 5-frame dice-reveal hold) into
    # one entry with a summed duration so ffmpeg doesn't open the file
    # repeatedly. The last filename is duplicated per concat-demuxer quirk:
    # without it the final entry's duration is silently dropped.
    if frames_dir is not None and frame_pngs and all(p is not None for p in frame_pngs):
        concat_lines: list[str] = []
        i = 0
        while i < len(frame_pngs):
            cur = frame_pngs[i]
            run_dur_ms = 0
            while i < len(frame_pngs) and frame_pngs[i] == cur:
                run_dur_ms += durations_per_frame[i]
                i += 1
            concat_lines.append(f"file '{cur.name}'")
            concat_lines.append(f"duration {run_dur_ms / 1000.0:.3f}")
        concat_lines.append(f"file '{frame_pngs[-1].name}'")
        (frames_dir / "concat.txt").write_text("\n".join(concat_lines) + "\n")

    return GifResult(path=out_path, impact_ms=impact_ms, total_ms=total_ms,
                     frames_dir=frames_dir)


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
