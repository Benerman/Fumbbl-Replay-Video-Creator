"""Dice extraction + iconography for tableaux.

Each Blood Bowl command can carry multiple dice rolls in its
`reportList.reports`. For pivotal-play tableaux we surface the dice
that produced the moment so the viewer can read the result alongside
the action.

Block dice (6 faces, per BlockDiceCategory.java in the FFB client):
   1 = PLAYERDOWN     attacker down (skull)
   2 = BOTHDOWN       both go down (skull + burst)
   3 = PUSHBACK1      defender pushed (blue up-arrow)
   4 = PUSHBACK2      defender pushed (same icon as 3)
   5 = STUMBLE        defender stumbles (outlined star)
   6 = POW            defender down (filled star)

We fetch the actual PNGs from FFB's resources repo and cache them on
disk - identical to how position icons are cached - so the rendered
dice match what coaches see in the FFB client.

Other rolls (dodge / GFI / pickup / pass / catch) are d6 1..6.
FFB ships no per-face d6 PNG (only a single generic d6 sprite); we
draw a numbered die inline.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import requests
from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger(__name__)

_BLOCK_FACE = {1: "SKULL", 2: "BOTH_DOWN", 3: "PUSH", 4: "PUSH", 5: "STUMBLE", 6: "POW"}

# FFB's official dice PNGs. Faces 3 and 4 share one file (the client
# cannot visually distinguish push-1 from push-2).
_FFB_RAW_BASE = (
    "https://raw.githubusercontent.com/christerk/ffb/master/"
    "ffb-resources/src/main/resources/icons/sidebar/dice"
)
_BLOCK_DIE_FILES = {
    1: "new_skool_black_1.png",
    2: "new_skool_black_2.png",
    3: "new_skool_black_3_4.png",
    4: "new_skool_black_3_4.png",
    5: "new_skool_black_5.png",
    6: "new_skool_black_6.png",
}

_CACHE_DIR = Path(os.environ.get(
    "FUMBBL_REPLAY_CACHE",
    str(Path.home() / ".cache" / "fumbbl-replay-video-creator")
)) / "dice"

_block_die_cache: dict[int, Image.Image] = {}


def fetch_block_die(face: int) -> Image.Image:
    """Return the FFB PNG for one block-die face (1..6). Cached on disk
    and in process."""
    if face in _block_die_cache:
        return _block_die_cache[face]
    filename = _BLOCK_DIE_FILES.get(face)
    if not filename:
        raise ValueError(f"no FFB icon for block die face {face!r}")
    cache_path = _CACHE_DIR / filename
    if not cache_path.exists():
        url = f"{_FFB_RAW_BASE}/{filename}"
        log.info("GET %s", url)
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(r.content)
    im = Image.open(cache_path).convert("RGBA")
    _block_die_cache[face] = im
    return im


@dataclass
class DiceGroup:
    """One block or d6 roll resolved during a command.

    `kind` distinguishes a block roll (multi-die, special faces) from a
    plain d6 roll. `actor_id` is the player rolling. `target` is the
    minimum needed (d6 only) or the defender id (block only).
    """
    kind: Literal["block", "d6"]
    label: str                  # "block" / "dodge" / "gfi" / "pickup" / "pass" / "catch"
    rolls: list[int]            # for blocks: list of face values 1..6; for d6: [roll]
    actor_id: str | None = None
    defender_id: str | None = None
    minimum: int | None = None  # d6: minimum to succeed
    successful: bool | None = None


def extract_for_command(
    replay: dict[str, Any],
    command_nr: int,
    *,
    lookback: int = 0,
) -> list[DiceGroup]:
    """Pull every dice-ish report from the window [command_nr-lookback..command_nr].

    Casualties live in a different command from the block that caused
    them: the block roll fires first, then several commands later the
    injury / casualty report. Pass `lookback` to widen the scan.

    Returns dice in chronological order. The acting-player attribution
    is sticky across the whole gameLog so dice from a command that
    didn't itself set `actingPlayerSetPlayerId` still know who rolled.
    """
    out: list[DiceGroup] = []
    cmds = replay.get("gameLog", {}).get("commandArray", []) or []
    start = command_nr - lookback
    acting: str | None = None
    for c in cmds:
        cn = int(c.get("commandNr", 0) or 0)
        if cn > command_nr:
            break
        # Update sticky acting-player state from EVERY command up to the target.
        for m in c.get("modelChangeList", {}).get("modelChangeArray", []) or []:
            if m.get("modelChangeId") == "actingPlayerSetPlayerId" and m.get("modelChangeValue"):
                acting = str(m["modelChangeValue"])
        if cn < start:
            continue
        for r in c.get("reportList", {}).get("reports", []) or []:
            rid = r.get("reportId")
            if rid == "blockRoll":
                roll = r.get("blockRoll") or []
                if not roll:
                    continue
                out.append(DiceGroup(
                    kind="block", label="block",
                    rolls=[int(v) for v in roll],
                    actor_id=acting,
                    defender_id=str(r.get("defenderId")) if r.get("defenderId") else None,
                ))
            elif rid in ("dodgeRoll", "goForItRoll", "pickUpRoll", "passRoll", "catchRoll"):
                label = {"dodgeRoll": "dodge", "goForItRoll": "gfi", "pickUpRoll": "pickup",
                         "passRoll": "pass", "catchRoll": "catch"}[rid]
                roll = r.get("roll")
                if roll is None:
                    continue
                out.append(DiceGroup(
                    kind="d6", label=label,
                    rolls=[int(roll)],
                    actor_id=str(r.get("playerId")) if r.get("playerId") else None,
                    minimum=int(r.get("minimumRoll")) if r.get("minimumRoll") else None,
                    successful=bool(r.get("successful")) if r.get("successful") is not None else None,
                ))
    return out


# ---------- icon drawing ----------

BLOCK_DIE_SIZE = 22
D6_SIZE = 20
DIE_BG = (245, 245, 240)
DIE_FG = (24, 24, 24)
DIE_SUCCESS = (60, 150, 70)
DIE_FAIL = (200, 60, 50)


def draw_block_die(canvas: Image.Image, x: int, y: int, value: int, *, size: int | None = None) -> None:
    """Paste the FFB block-die PNG at the given top-left corner."""
    target = size or BLOCK_DIE_SIZE
    try:
        die = fetch_block_die(value)
    except Exception as e:
        log.warning("falling back to drawn die for face %s: %s", value, e)
        _draw_fallback_die(canvas, x, y, value, target)
        return
    scale = target / max(die.size)
    new_size = (max(1, int(die.size[0] * scale)), max(1, int(die.size[1] * scale)))
    resized = die.resize(new_size, resample=Image.LANCZOS) if scale != 1.0 else die
    sw, sh = resized.size
    canvas.alpha_composite(resized, (x + (target - sw) // 2, y + (target - sh) // 2))


def _draw_fallback_die(canvas: Image.Image, x: int, y: int, value: int, s: int) -> None:
    """Used only when the FFB PNG fetch fails (offline / 404)."""
    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle([x, y, x + s, y + s], radius=4, fill=DIE_BG, outline=(40, 40, 40))
    draw.text((x + s // 2 - 4, y + s // 2 - 6), str(value), fill=DIE_FG)


def draw_d6(canvas: Image.Image, x: int, y: int, value: int, *, success: bool | None = None,
             font: ImageFont.ImageFont | None = None) -> None:
    s = D6_SIZE
    draw = ImageDraw.Draw(canvas)
    border = DIE_SUCCESS if success else DIE_FAIL if success is False else (40, 40, 40)
    width = 2 if success is not None else 1
    draw.rounded_rectangle([x, y, x + s, y + s], radius=3, fill=DIE_BG, outline=border, width=width)
    f = font or ImageFont.load_default()
    txt = str(value)
    try:
        l, t, r, b = draw.textbbox((0, 0), txt, font=f)
        tw, th = r - l, b - t
    except AttributeError:
        tw, th = draw.textsize(txt, font=f)
    draw.text((x + (s - tw) / 2, y + (s - th) / 2 - 1), txt, fill=DIE_FG, font=f)


def render_group_strip(group: DiceGroup, font: ImageFont.ImageFont | None = None) -> Image.Image:
    """Render one DiceGroup as a small horizontal strip on a transparent canvas.

    Strip layout: [die][die]... with 2px between, plus an optional
    success/fail border colour for d6 rolls.
    """
    if group.kind == "block":
        s = BLOCK_DIE_SIZE
        gap = 2
        w = len(group.rolls) * s + (len(group.rolls) - 1) * gap
        canvas = Image.new("RGBA", (w + 4, s + 4), (0, 0, 0, 0))
        x = 2
        for v in group.rolls:
            draw_block_die(canvas, x, 2, v)
            x += s + gap
        return canvas
    # d6
    s = D6_SIZE
    canvas = Image.new("RGBA", (s + 4, s + 4), (0, 0, 0, 0))
    draw_d6(canvas, 2, 2, group.rolls[0], success=group.successful, font=font)
    return canvas
