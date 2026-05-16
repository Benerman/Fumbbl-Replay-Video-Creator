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
# FFB shows a slash on PRONE figures and an X on STUNNED. Other transient
# down-states (just-blocked, mid-fall) get the slash too since they will
# settle into PRONE next, or into STUNNED if KO'd.
_PRONE_STATES = {_STATE_PRONE, _STATE_BLOCKED, _STATE_FALLING, _STATE_HIT_GROUND}

# Marker colours: a bright red the FFB client uses.
_MARKER_COLOR = (235, 40, 35)
_MARKER_WIDTH = 3
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
    *,
    home_name: str | None = None,
    away_name: str | None = None,
    home_logo: Image.Image | None = None,
    away_logo: Image.Image | None = None,
) -> Path:
    """Render a vertical pitch tableau.

    Coordinate convention: FFB stores positions as (x, y) where x is
    the long axis (0..25) and y is the short axis (0..14). In this
    renderer we put the long axis VERTICALLY - BB x=0 is the top
    endzone, BB x=25 is the bottom endzone - so screen pixels are
    `sx = MARGIN_X + y * TILE` and `sy = MARGIN_TOP + x * TILE`.
    """
    targets = _targets_for_play(play)
    sprites = sprites or {}

    # Vertical layout: 15 cols × 26 rows of tiles
    pitch_w = PITCH_HEIGHT * TILE     # screen width = BB y-axis (15)
    pitch_h = PITCH_WIDTH * TILE      # screen height = BB x-axis (26)
    img_w = pitch_w + 2 * MARGIN_X
    img_h = pitch_h + MARGIN_TOP + CAPTION_H

    img = Image.new("RGBA", (img_w, img_h), (24, 30, 24, 255))
    draw = ImageDraw.Draw(img)
    font = _font(13)
    small = _font(11)
    tiny = _font(9)
    endzone_font = _font(20)

    _draw_pitch(draw, MARGIN_X, MARGIN_TOP, pitch_w, pitch_h)
    _draw_endzone_labels(draw, MARGIN_X, MARGIN_TOP, pitch_w,
                          home_name, away_name, endzone_font)
    _paste_logos(img, MARGIN_X, MARGIN_TOP, pitch_w, home_logo, away_logo)

    # Header strip: who played, half, turn, score
    header = _header_text(play)
    draw.text((MARGIN_X, 14), header, fill=TEXT, font=font)

    # Ball
    if state.ball:
        bx, by = state.ball
        if 0 <= bx < PITCH_WIDTH and 0 <= by < PITCH_HEIGHT:
            cx = MARGIN_X + by * TILE + TILE // 2
            cy = MARGIN_TOP + bx * TILE + TILE // 2
            r = TILE // 4
            draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=BALL_COLOR)

    # Players on pitch
    involved = targets.involved()
    for pid, (x, y) in state.on_pitch().items():
        info = player_lookup.get(pid)
        side = info.side if info else "home"
        color = HOME_COLOR if side == "home" else AWAY_COLOR
        # Rotated: BB y -> screen x, BB x -> screen y.
        cx = MARGIN_X + y * TILE + TILE // 2
        cy = MARGIN_TOP + x * TILE + TILE // 2
        r = TILE // 2 - 3
        sprite = sprites.get(pid)
        # Down-state visualisation: low 4 bits of player_states encode the base state.
        # The FFB client overlays a "/" slash on prone players and an "X" on stunned
        # players, leaving the sprite upright. We do the same.
        base_state = (state.player_states.get(pid, 0) or 0) & 0xF
        is_prone = base_state in _PRONE_STATES
        is_stunned = base_state == _STATE_STUNNED
        is_dead = base_state in (_STATE_KO, _STATE_BH, _STATE_SI, _STATE_RIP)
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
            if is_dead:
                # KO / casualty in the transitional pre-dugout-move state: dim it.
                sprite_resized = _dim(sprite_resized)
            sw, sh = sprite_resized.size
            img.paste(sprite_resized, (cx - sw // 2, cy - sh // 2), sprite_resized)
            if is_prone:
                _draw_prone_slash(draw, cx, cy, r)
            elif is_stunned:
                _draw_stun_x(draw, cx, cy, r)
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
            if is_prone:
                _draw_prone_slash(draw, cx, cy, r)
            elif is_stunned:
                _draw_stun_x(draw, cx, cy, r)

    # Caption: weight + play headline, wrapped to fit the narrow vertical canvas.
    weight_str = f"[{play.weight:.2f}]"
    cap_y = MARGIN_TOP + pitch_h + 8
    draw.text((MARGIN_X, cap_y), weight_str, fill=DIM_TEXT, font=font)
    wt_w, _ = _text_size(draw, weight_str, font)
    caption_x = MARGIN_X + wt_w + 6
    max_caption_w = img_w - caption_x - MARGIN_X
    lines = _wrap_text(draw, play.headline(), font, max_caption_w)
    for i, line in enumerate(lines[:3]):
        draw.text((caption_x, cap_y + i * 14), line, fill=TEXT, font=font)

    n_off = len(state.off_pitch())
    if n_off:
        draw.text((MARGIN_X, cap_y + len(lines) * 14 + 4),
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
    """Vertical pitch: BB x runs top-to-bottom, BB y runs left-to-right.

    Endzones are the top and bottom rows; line of scrimmage is the
    horizontal line at BB x=13; wide zones are the two vertical lines
    at BB y=4 and BB y=11.
    """
    draw.rectangle([ox, oy, ox + w, oy + h], fill=PITCH_GREEN)
    # Endzones: top (BB x=0) and bottom (BB x=25) rows, full pitch width.
    draw.rectangle([ox, oy, ox + w, oy + TILE], fill=ENDZONE_TINT)
    draw.rectangle([ox, oy + h - TILE, ox + w, oy + h], fill=ENDZONE_TINT)
    # Wide zones: vertical separator lines at BB y=4 and y=11.
    wide_left = ox + 4 * TILE
    wide_right = ox + 11 * TILE
    draw.line([wide_left, oy, wide_left, oy + h], fill=WIDE_TINT, width=1)
    draw.line([wide_right, oy, wide_right, oy + h], fill=WIDE_TINT, width=1)
    # Line of scrimmage: horizontal line at BB x=13.
    los = oy + 13 * TILE
    draw.line([ox, los, ox + w, los], fill=PITCH_LINE, width=2)
    # Subtle grid: PITCH_HEIGHT (15) vertical lines, PITCH_WIDTH (26) horizontal lines.
    for col in range(1, PITCH_HEIGHT):
        gx = ox + col * TILE
        draw.line([gx, oy, gx, oy + h], fill=(48, 100, 56), width=1)
    for row in range(1, PITCH_WIDTH):
        gy = oy + row * TILE
        draw.line([ox, gy, ox + w, gy], fill=(48, 100, 56), width=1)


def _draw_endzone_labels(
    draw: ImageDraw.ImageDraw,
    ox: int, oy: int, w: int,
    home_name: str | None,
    away_name: str | None,
    font,
) -> None:
    """Stamp team names in the endzones. Home goes top, away goes bottom."""
    if home_name:
        _centered_text(draw, home_name, ox, oy, w, TILE, HOME_COLOR, font)
    if away_name:
        # Bottom endzone is the LAST row.
        _centered_text(draw, away_name, ox, oy + (PITCH_WIDTH - 1) * TILE, w, TILE,
                       AWAY_COLOR, font)


def _centered_text(draw, text: str, ox: int, oy: int, w: int, h: int,
                    color, font) -> None:
    tw, th = _text_size(draw, text, font)
    draw.text((ox + (w - tw) // 2, oy + (h - th) // 2), text, fill=color, font=font)


def _paste_logos(
    img: Image.Image,
    ox: int, oy: int, w: int,
    home_logo: Image.Image | None,
    away_logo: Image.Image | None,
) -> None:
    """Drop a faded team logo as a watermark in each half of the pitch.

    Home half = top (BB x=1..12), away half = bottom (BB x=13..24).
    """
    # Logo fits roughly within a 5×5 tile area, centred on the half.
    logo_target = 5 * TILE
    if home_logo is not None:
        _paste_centered_logo(img, home_logo,
                              ox + w // 2,
                              oy + (1 + 6) * TILE,  # centre of rows 1..12 is row ~6
                              logo_target)
    if away_logo is not None:
        _paste_centered_logo(img, away_logo,
                              ox + w // 2,
                              oy + (13 + 6) * TILE,  # centre of rows 13..24
                              logo_target)


def _paste_centered_logo(canvas: Image.Image, logo: Image.Image,
                          cx: int, cy: int, target_size: int) -> None:
    if logo.mode != "RGBA":
        logo = logo.convert("RGBA")
    scale = target_size / max(logo.size)
    new_size = (max(1, int(logo.size[0] * scale)), max(1, int(logo.size[1] * scale)))
    logo_resized = logo.resize(new_size, resample=Image.LANCZOS)
    # Fade to ~35% opacity for watermark feel.
    alpha = logo_resized.split()[-1]
    alpha = alpha.point(lambda a: int(a * 0.35))
    logo_resized.putalpha(alpha)
    canvas.paste(logo_resized, (cx - new_size[0] // 2, cy - new_size[1] // 2), logo_resized)


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


def _wrap_text(draw, text: str, font, max_width: int) -> list[str]:
    """Greedy word-wrap; collapses to multiple lines that each fit max_width."""
    words = text.split()
    lines: list[str] = []
    cur = ""
    for w in words:
        candidate = (cur + " " + w).strip()
        tw, _ = _text_size(draw, candidate, font)
        if tw <= max_width or not cur:
            cur = candidate
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def _draw_prone_slash(draw: ImageDraw.ImageDraw, cx: int, cy: int, r: int) -> None:
    """Bright red slash across the player — matches the FFB client's prone marker."""
    # Black outline first, then the red marker on top — keeps the X / slash
    # readable on both blue and red sprites.
    draw.line([cx - r, cy + r, cx + r, cy - r], fill=(0, 0, 0), width=_MARKER_WIDTH + 2)
    draw.line([cx - r, cy + r, cx + r, cy - r], fill=_MARKER_COLOR, width=_MARKER_WIDTH)


def _draw_stun_x(draw: ImageDraw.ImageDraw, cx: int, cy: int, r: int) -> None:
    """Bright red X across the player — matches the FFB client's stunned marker."""
    draw.line([cx - r, cy - r, cx + r, cy + r], fill=(0, 0, 0), width=_MARKER_WIDTH + 2)
    draw.line([cx - r, cy + r, cx + r, cy - r], fill=(0, 0, 0), width=_MARKER_WIDTH + 2)
    draw.line([cx - r, cy - r, cx + r, cy + r], fill=_MARKER_COLOR, width=_MARKER_WIDTH)
    draw.line([cx - r, cy + r, cx + r, cy - r], fill=_MARKER_COLOR, width=_MARKER_WIDTH)


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
