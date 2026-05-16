"""Dice extraction + iconography for tableaux.

Each Blood Bowl command can carry multiple dice rolls in its
`reportList.reports`. For pivotal-play tableaux we surface the dice
that produced the moment so the viewer can read the result alongside
the action.

Block dice (6 faces):
   1 = SKULL          attacker down
   2 = BOTH_DOWN      both go down
   3 = PUSH           defender pushed
   4 = PUSH           defender pushed (same face as 3)
   5 = STUMBLE        defender stumbles
   6 = POW            defender down

Other rolls (dodge / GFI / pickup / pass / catch) are d6: 1..6.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from PIL import Image, ImageDraw, ImageFont


_BLOCK_FACE = {1: "SKULL", 2: "BOTH_DOWN", 3: "PUSH", 4: "PUSH", 5: "STUMBLE", 6: "POW"}


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


def draw_block_die(canvas: Image.Image, x: int, y: int, value: int, *, ring_color=None) -> None:
    """Render a single block die at the given top-left corner."""
    s = BLOCK_DIE_SIZE
    draw = ImageDraw.Draw(canvas)
    # rounded rectangle
    draw.rounded_rectangle([x, y, x + s, y + s], radius=4, fill=DIE_BG,
                            outline=(ring_color or (40, 40, 40)),
                            width=2 if ring_color else 1)
    face = _BLOCK_FACE.get(value, "?")
    cx, cy = x + s // 2, y + s // 2
    if face == "SKULL":
        # Two eye holes, no mouth curve - reads as ominous, not a smiley.
        # Tiny teeth bars below to seal the skull look.
        draw.ellipse([cx - 5, cy - 5, cx - 2, cy - 1], fill=DIE_FG)
        draw.ellipse([cx + 2, cy - 5, cx + 5, cy - 1], fill=DIE_FG)
        draw.line([cx - 4, cy + 3, cx - 4, cy + 6], fill=DIE_FG, width=1)
        draw.line([cx - 1, cy + 3, cx - 1, cy + 6], fill=DIE_FG, width=1)
        draw.line([cx + 2, cy + 3, cx + 2, cy + 6], fill=DIE_FG, width=1)
    elif face == "BOTH_DOWN":
        # cross of two diagonal lines
        draw.line([cx - 6, cy - 6, cx + 6, cy + 6], fill=DIE_FG, width=2)
        draw.line([cx - 6, cy + 6, cx + 6, cy - 6], fill=DIE_FG, width=2)
    elif face == "PUSH":
        # right-pointing arrow
        draw.line([cx - 6, cy, cx + 5, cy], fill=DIE_FG, width=2)
        draw.polygon([(cx + 5, cy - 3), (cx + 5, cy + 3), (cx + 9 - 1, cy)], fill=DIE_FG)
    elif face == "STUMBLE":
        # push arrow with a chevron at the base
        draw.line([cx - 6, cy, cx + 5, cy], fill=DIE_FG, width=2)
        draw.polygon([(cx + 5, cy - 3), (cx + 5, cy + 3), (cx + 9 - 1, cy)], fill=DIE_FG)
        draw.line([cx - 7, cy - 4, cx - 5, cy], fill=DIE_FG, width=2)
        draw.line([cx - 7, cy + 4, cx - 5, cy], fill=DIE_FG, width=2)
    elif face == "POW":
        # star burst
        for ang in range(0, 360, 45):
            from math import cos, sin, radians
            ex = int(cx + 7 * cos(radians(ang)))
            ey = int(cy + 7 * sin(radians(ang)))
            draw.line([cx, cy, ex, ey], fill=DIE_FG, width=2)
    else:
        draw.text((cx - 4, cy - 5), "?", fill=DIE_FG)


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
