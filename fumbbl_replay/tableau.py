"""Render a single pivotal play as a PNG tableau.

Spike-quality output: a green pitch grid with team-coloured tokens
at each on-pitch player's coordinates, the ball as a white circle,
and a caption strip with the pivotal-play headline. Involved players
(scorer / victim / inflicter) get a yellow ring.

Visual identity is deliberately stub - the goal is to confirm the
data wiring (field reconstruction, player roster, highlight targeting)
before committing to sprite assets.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from PIL import Image, ImageDraw, ImageFont

from .analyzer import PivotalPlay
from .events import PlayerInfo
from .field_state import FieldState, PITCH_WIDTH, PITCH_HEIGHT


# Tile size in pixels per pitch square.
TILE = 28

# Player-state base values (low 4 bits of the bitmask FFB stores).
_STATE_STANDING = 1
_STATE_MOVING = 2
_STATE_PRONE = 3
_STATE_STUNNED = 4
_STATE_KO = 5
_STATE_BH = 6
_STATE_SI = 7
_STATE_RIP = 8
_STATE_BLOCKED = 11
_STATE_FALLING = 12
_STATE_HIT_GROUND = 13
_DOWN_STATES = {_STATE_PRONE, _STATE_STUNNED, _STATE_KO, _STATE_BH, _STATE_SI, _STATE_RIP,
                _STATE_BLOCKED, _STATE_FALLING, _STATE_HIT_GROUND}
# Margins around the pitch.
MARGIN_X = 20
MARGIN_TOP = 50
CAPTION_H = 70
# Field colours.
PITCH_GREEN = (40, 90, 50)
PITCH_LINE = (200, 220, 200)
ENDZONE_TINT = (60, 110, 60)
WIDE_TINT = (50, 100, 60)
HOME_COLOR = (60, 110, 200)
AWAY_COLOR = (200, 70, 60)
HIGHLIGHT = (255, 215, 0)
BALL_COLOR = (240, 240, 240)
TEXT = (240, 240, 230)
DIM_TEXT = (180, 180, 170)


@dataclass
class TableauTargets:
    """Player ids the renderer should ring-highlight."""
    scorer: str | None = None
    victim: str | None = None
    inflicter: str | None = None

    def involved(self) -> set[str]:
        return {p for p in (self.scorer, self.victim, self.inflicter) if p}


def render_tableau(
    play: PivotalPlay,
    state: FieldState,
    player_lookup: dict[str, PlayerInfo],
    out_path: Path,
    sprites: dict[str, Image.Image] | None = None,
) -> Path:
    targets = _targets_for_play(play)
    sprites = sprites or {}

    pitch_w = PITCH_WIDTH * TILE
    pitch_h = PITCH_HEIGHT * TILE
    img_w = pitch_w + 2 * MARGIN_X
    img_h = pitch_h + MARGIN_TOP + CAPTION_H

    img = Image.new("RGBA", (img_w, img_h), (24, 30, 24, 255))
    draw = ImageDraw.Draw(img)
    font = _font(14)
    small = _font(11)
    tiny = _font(9)

    _draw_pitch(draw, MARGIN_X, MARGIN_TOP, pitch_w, pitch_h)

    # Header strip: who played, half, turn, score
    header = _header_text(play)
    draw.text((MARGIN_X, 14), header, fill=TEXT, font=font)

    # Ball
    if state.ball:
        bx, by = state.ball
        if 0 <= bx < PITCH_WIDTH and 0 <= by < PITCH_HEIGHT:
            cx = MARGIN_X + bx * TILE + TILE // 2
            cy = MARGIN_TOP + by * TILE + TILE // 2
            r = TILE // 4
            draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=BALL_COLOR)

    # Players on pitch
    involved = targets.involved()
    for pid, (x, y) in state.on_pitch().items():
        info = player_lookup.get(pid)
        side = info.side if info else "home"
        color = HOME_COLOR if side == "home" else AWAY_COLOR
        cx = MARGIN_X + x * TILE + TILE // 2
        cy = MARGIN_TOP + y * TILE + TILE // 2
        r = TILE // 2 - 3
        sprite = sprites.get(pid)
        # Down-state visualisation: low 4 bits of player_states encode the base state.
        # 3=PRONE 4=STUNNED 5=KO 6=BH 7=SI 8=RIP 11=BLOCKED 12=FALLING 13=HIT_ON_GROUND.
        # Anything other than 1/2 (STANDING/MOVING) gets rendered as down.
        base_state = (state.player_states.get(pid, 0) or 0) & 0xF
        is_down = base_state in _DOWN_STATES
        is_stunned = base_state == _STATE_STUNNED
        if pid in involved:
            ring_r = r + (5 if sprite else 4)
            draw.ellipse([cx - ring_r, cy - ring_r, cx + ring_r, cy + ring_r], fill=HIGHLIGHT)
        if sprite:
            # Coloured disc behind the sprite for team identification.
            disc_r = r + 1
            draw.ellipse([cx - disc_r, cy - disc_r, cx + disc_r, cy + disc_r], fill=color + (255,))
            sw, sh = sprite.size
            scale = (TILE - 4) / max(sw, sh)
            if scale != 1.0:
                sprite_resized = sprite.resize((max(1, int(sw * scale)), max(1, int(sh * scale))),
                                                resample=Image.NEAREST)
            else:
                sprite_resized = sprite
            if is_down:
                sprite_resized = sprite_resized.rotate(-90, expand=True, resample=Image.NEAREST)
                # Dim a knocked-out figure so it reads "off the action".
                if base_state in (_STATE_STUNNED, _STATE_KO, _STATE_BH, _STATE_SI, _STATE_RIP):
                    sprite_resized = _dim(sprite_resized)
            sw, sh = sprite_resized.size
            img.paste(sprite_resized, (cx - sw // 2, cy - sh // 2), sprite_resized)
            if is_stunned:
                # Yellow asterisk above the head for "stunned" — distinct from plain prone.
                _draw_stun_marker(draw, cx, cy - r, font)
            # Tiny jersey number badge in the bottom-right corner.
            if info and info.number is not None:
                label = str(info.number)
                tw, th = _text_size(draw, label, tiny)
                bx_, by_ = cx + r - tw - 1, cy + r - th
                draw.rectangle([bx_ - 1, by_ - 1, bx_ + tw + 1, by_ + th + 1], fill=(0, 0, 0, 200))
                draw.text((bx_, by_), label, fill=(255, 255, 255), font=tiny)
        else:
            draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=color)
            if info and info.number is not None:
                label = str(info.number)
                tw, th = _text_size(draw, label, small)
                draw.text((cx - tw / 2, cy - th / 2), label, fill=(255, 255, 255), font=small)
            if is_down:
                # Without a sprite we draw a pale horizontal bar across the disc to
                # convey "down".
                draw.line([cx - r, cy, cx + r, cy], fill=(20, 20, 20), width=2)
            if is_stunned:
                _draw_stun_marker(draw, cx, cy - r, font)

    # Caption: the play headline.
    caption = play.headline()
    weight_str = f"[{play.weight:.2f}]"
    cap_y = MARGIN_TOP + pitch_h + 12
    draw.text((MARGIN_X, cap_y), weight_str, fill=DIM_TEXT, font=font)
    wt_w, _ = _text_size(draw, weight_str, font)
    draw.text((MARGIN_X + wt_w + 8, cap_y), caption, fill=TEXT, font=font)

    n_off = len(state.off_pitch())
    if n_off:
        draw.text((MARGIN_X, cap_y + 20),
                  f"({n_off} players off-pitch / in dugout)",
                  fill=DIM_TEXT, font=small)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.convert("RGB").save(out_path)
    return out_path


def _targets_for_play(play: PivotalPlay) -> TableauTargets:
    if play.kind == "touchdown":
        return TableauTargets(scorer=play.player_id)
    if play.kind == "interception":
        return TableauTargets(scorer=play.player_id)
    return TableauTargets(victim=play.player_id, inflicter=play.inflicter_id)


def _draw_pitch(draw: ImageDraw.ImageDraw, ox: int, oy: int, w: int, h: int) -> None:
    draw.rectangle([ox, oy, ox + w, oy + h], fill=PITCH_GREEN)
    # Endzones (1 column each side)
    draw.rectangle([ox, oy, ox + TILE, oy + h], fill=ENDZONE_TINT)
    draw.rectangle([ox + w - TILE, oy, ox + w, oy + h], fill=ENDZONE_TINT)
    # Wide zones (4 rows top + 4 rows bottom in BB)
    wide_top = oy + 4 * TILE
    wide_bot = oy + 11 * TILE
    draw.line([ox, wide_top, ox + w, wide_top], fill=WIDE_TINT, width=1)
    draw.line([ox, wide_bot, ox + w, wide_bot], fill=WIDE_TINT, width=1)
    # Line of scrimmage (between x=12 and x=13 in BB)
    los = ox + 13 * TILE
    draw.line([los, oy, los, oy + h], fill=PITCH_LINE, width=2)
    # Subtle grid
    for x in range(1, PITCH_WIDTH):
        gx = ox + x * TILE
        draw.line([gx, oy, gx, oy + h], fill=(48, 100, 56), width=1)
    for y in range(1, PITCH_HEIGHT):
        gy = oy + y * TILE
        draw.line([ox, gy, ox + w, gy], fill=(48, 100, 56), width=1)


def _header_text(p: PivotalPlay) -> str:
    bits = [p.team_name, "vs", p.against_team]
    if p.score_home is not None and p.score_away is not None:
        bits.append(f"  {p.score_home}-{p.score_away}")
    if p.half:
        bits.append(f"  half {p.half}")
    if p.turn:
        bits.append(f"  turn {p.turn}")
    return " ".join(bits)


def _dim(im: Image.Image) -> Image.Image:
    """Knock the brightness down on a sprite to mark it as out-of-action."""
    out = im.copy()
    if out.mode != "RGBA":
        out = out.convert("RGBA")
    pixels = out.load()
    for j in range(out.size[1]):
        for i in range(out.size[0]):
            r, g, b, a = pixels[i, j]
            pixels[i, j] = (r * 6 // 10, g * 6 // 10, b * 6 // 10, a)
    return out


def _draw_stun_marker(draw: ImageDraw.ImageDraw, cx: int, cy: int, font) -> None:
    """Yellow asterisk just above the player's head — universal "stunned"."""
    draw.text((cx - 4, cy - 12), "*", fill=HIGHLIGHT, font=font)


def _font(size: int) -> ImageFont.ImageFont:
    # Pillow's default bitmap font is fixed-size; fall back to that.
    # We try a couple of common system fonts first for legibility.
    for path in (
        "/System/Library/Fonts/Helvetica.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/Library/Fonts/Arial.ttf",
    ):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _text_size(draw: ImageDraw.ImageDraw, text: str, font) -> tuple[int, int]:
    """Pillow API for measuring text changed across versions; this works on both."""
    if hasattr(draw, "textbbox"):
        l, t, r, b = draw.textbbox((0, 0), text, font=font)
        return r - l, b - t
    return draw.textsize(text, font=font)
