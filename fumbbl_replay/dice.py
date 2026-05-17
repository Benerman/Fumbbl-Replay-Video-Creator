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
    """One dice resolution during a command.

    `kind` distinguishes block (multi-die, special faces), plain d6
    (one die, success threshold), and 2d6 rolls (armor + injury, where
    the result is a sum vs threshold).
    """
    kind: Literal["block", "d6", "2d6"]
    label: str                  # "block" / "dodge" / "gfi" / "pickup" / "pass" / "catch" / "armor" / "injury"
    rolls: list[int]            # block: 1..6 face values; d6: [roll]; 2d6: [die1, die2]
    actor_id: str | None = None
    defender_id: str | None = None
    minimum: int | None = None  # d6/2d6: minimum to succeed (sum for 2d6)
    successful: bool | None = None
    # Caption shown under the strip in the renderer ("armor", "injury", etc.).
    caption: str | None = None


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
            elif rid == "injury":
                # FFB emits two `injury` reports per casualty event:
                # one for armor+injury (skipInjuryParts="CAS") and a
                # second for the casualty phase only
                # (skipInjuryParts="EVERYTHING_BUT_CAS"), both carrying
                # the same armor/injury dice. We only want the first;
                # the casualty severity is already surfaced via the
                # slash/X marker on the victim.
                if r.get("skipInjuryParts") == "EVERYTHING_BUT_CAS":
                    continue
                victim = str(r.get("defenderId")) if r.get("defenderId") else None
                armor_roll = r.get("armorRoll") or []
                if len(armor_roll) == 2:
                    out.append(DiceGroup(
                        kind="2d6", label="armor",
                        rolls=[int(armor_roll[0]), int(armor_roll[1])],
                        actor_id=acting, defender_id=victim,
                        successful=bool(r.get("armorBroken")) if r.get("armorBroken") is not None else None,
                        caption="armor",
                    ))
                injury_roll = r.get("injuryRoll") or []
                if r.get("armorBroken") and len(injury_roll) == 2:
                    out.append(DiceGroup(
                        kind="2d6", label="injury",
                        rolls=[int(injury_roll[0]), int(injury_roll[1])],
                        actor_id=acting, defender_id=victim,
                        successful=None,
                        caption="injury",
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

BLOCK_DIE_SIZE = 44
D6_SIZE = 40
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
    caption underneath ("armor" / "injury") for 2d6 groups.
    """
    caption = group.caption
    caption_h = 24 if caption else 0
    if group.kind == "block":
        s = BLOCK_DIE_SIZE
        gap = 2
        w = len(group.rolls) * s + (len(group.rolls) - 1) * gap
        canvas = Image.new("RGBA", (w + 4, s + 4 + caption_h), (0, 0, 0, 0))
        x = 2
        for v in group.rolls:
            draw_block_die(canvas, x, 2, v)
            x += s + gap
    elif group.kind == "2d6":
        s = D6_SIZE
        gap = 2
        w = 2 * s + gap
        canvas = Image.new("RGBA", (w + 4, s + 4 + caption_h), (0, 0, 0, 0))
        for i, v in enumerate(group.rolls):
            # For armor: success border = armor broken (bad for victim, "successful" attack).
            draw_d6(canvas, 2 + i * (s + gap), 2, v, success=group.successful, font=font)
    else:  # d6
        s = D6_SIZE
        canvas = Image.new("RGBA", (s + 4, s + 4 + caption_h), (0, 0, 0, 0))
        draw_d6(canvas, 2, 2, group.rolls[0], success=group.successful, font=font)
    if caption:
        d = ImageDraw.Draw(canvas)
        # Small black-on-translucent caption strip under the dice.
        cap_y = canvas.size[1] - caption_h
        d.rectangle([0, cap_y, canvas.size[0], canvas.size[1]], fill=(0, 0, 0, 160))
        cap_font = ImageFont.load_default() if font is None else font
        try:
            l, t, r, b = d.textbbox((0, 0), caption, font=cap_font)
            tw = r - l
        except AttributeError:
            tw, _ = d.textsize(caption, font=cap_font)
        d.text(((canvas.size[0] - tw) / 2, cap_y + 1), caption, fill=(245, 245, 240), font=cap_font)
    return canvas
