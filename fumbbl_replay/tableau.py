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


@dataclass
class Layout:
    """Geometry for either orientation. `bb_to_screen(x, y)` is the only
    coordinate transform the rest of the renderer needs to call."""
    orientation: str           # "vertical" | "horizontal"
    cols: int                  # tile columns in image space
    rows: int                  # tile rows
    pitch_w: int               # px
    pitch_h: int               # px
    ox: int                    # pitch origin x
    oy: int                    # pitch origin y
    img_w: int
    img_h: int

    def bb_to_screen(self, bb_x: int, bb_y: int) -> tuple[int, int]:
        """Centre of the tile for BB (x, y)."""
        if self.orientation == "vertical":
            return (self.ox + bb_y * TILE + TILE // 2,
                    self.oy + bb_x * TILE + TILE // 2)
        return (self.ox + bb_x * TILE + TILE // 2,
                self.oy + bb_y * TILE + TILE // 2)


def _layout(orientation: str) -> Layout:
    if orientation == "vertical":
        cols, rows = PITCH_HEIGHT, PITCH_WIDTH       # 15 × 26
    else:
        cols, rows = PITCH_WIDTH, PITCH_HEIGHT       # 26 × 15
    pitch_w = cols * TILE
    pitch_h = rows * TILE
    img_w = pitch_w + 2 * MARGIN_X
    img_h = pitch_h + MARGIN_TOP + CAPTION_H
    return Layout(orientation, cols, rows, pitch_w, pitch_h,
                   MARGIN_X, MARGIN_TOP, img_w, img_h)


def render_tableau(
    play: PivotalPlay,
    state: FieldState,
    player_lookup: dict[str, PlayerInfo],
    out_path: Path,
    sprites: dict[str, Image.Image] | None = None,
    *,
    orientation: str = "vertical",
    home_name: str | None = None,
    away_name: str | None = None,
    home_logo: Image.Image | None = None,
    away_logo: Image.Image | None = None,
) -> Path:
    """Render one pivotal-play tableau.

    Layers, bottom to top: pitch base -> logo watermark -> endzone
    labels -> ball -> players + markers -> caption / header.
    """
    if orientation not in ("vertical", "horizontal"):
        raise ValueError(f"orientation must be 'vertical' or 'horizontal', got {orientation!r}")
    targets = _targets_for_play(play)
    sprites = sprites or {}
    lay = _layout(orientation)

    img = Image.new("RGBA", (lay.img_w, lay.img_h), (24, 30, 24, 255))
    draw = ImageDraw.Draw(img)
    font = _font(13)
    small = _font(11)
    tiny = _font(9)
    endzone_font = _font(20 if orientation == "vertical" else 16)

    # Layer 1: pitch base (grass, grid, endzone tint, LoS, wide zones).
    _draw_pitch(draw, lay)
    # Layer 2: team logo watermark (sits on the pitch but under everything else).
    _paste_logos(img, lay, home_logo, away_logo)
    # Layer 3: endzone team-name labels (drawn after logos so the text isn't washed).
    _draw_endzone_labels(img, draw, lay, home_name, away_name, endzone_font)
    # Layer 4: header bar above the pitch.
    draw.text((lay.ox, 14), _header_text(play), fill=TEXT, font=font)
    # Layer 5: ball.
    _draw_ball(draw, lay, state)
    # Layer 6: players + their state markers + the highlight ring.
    _draw_players(img, draw, lay, state, player_lookup, sprites, targets, tiny, small)
    # Layer 7: caption strip.
    _draw_caption(draw, lay, play, state, font, small)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.convert("RGB").save(out_path)
    return out_path


def _draw_ball(draw: ImageDraw.ImageDraw, lay: Layout, state: FieldState) -> None:
    if not state.ball:
        return
    bx, by = state.ball
    if not (0 <= bx < PITCH_WIDTH and 0 <= by < PITCH_HEIGHT):
        return
    cx, cy = lay.bb_to_screen(bx, by)
    r = TILE // 4
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=BALL_COLOR)


def _draw_players(
    img: Image.Image,
    draw: ImageDraw.ImageDraw,
    lay: Layout,
    state: FieldState,
    player_lookup: dict[str, PlayerInfo],
    sprites: dict[str, Image.Image],
    targets: "TableauTargets",
    tiny, small,
) -> None:
    involved = targets.involved()
    for pid, (x, y) in state.on_pitch().items():
        info = player_lookup.get(pid)
        side = info.side if info else "home"
        color = HOME_COLOR if side == "home" else AWAY_COLOR
        cx, cy = lay.bb_to_screen(x, y)
        r = TILE // 2 - 3
        sprite = sprites.get(pid)
        base_state = (state.player_states.get(pid, 0) or 0) & 0xF
        is_prone = base_state in _PRONE_STATES
        is_stunned = base_state == _STATE_STUNNED
        is_dead = base_state in (_STATE_KO, _STATE_BH, _STATE_SI, _STATE_RIP)
        if pid in involved:
            ring_r = r + (5 if sprite else 4)
            draw.ellipse([cx - ring_r, cy - ring_r, cx + ring_r, cy + ring_r], fill=HIGHLIGHT)
        if sprite:
            disc_r = r + 1
            draw.ellipse([cx - disc_r, cy - disc_r, cx + disc_r, cy + disc_r], fill=color + (255,))
            sw, sh = sprite.size
            scale = (TILE - 4) / max(sw, sh)
            sprite_resized = sprite.resize((max(1, int(sw * scale)), max(1, int(sh * scale))),
                                            resample=Image.NEAREST) if scale != 1.0 else sprite
            if is_dead:
                sprite_resized = _dim(sprite_resized)
            sw, sh = sprite_resized.size
            img.paste(sprite_resized, (cx - sw // 2, cy - sh // 2), sprite_resized)
            if is_prone:
                _draw_prone_slash(draw, cx, cy, r)
            elif is_stunned:
                _draw_stun_x(draw, cx, cy, r)
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


def _draw_caption(draw, lay: Layout, play: PivotalPlay, state: FieldState, font, small) -> None:
    weight_str = f"[{play.weight:.2f}]"
    cap_y = lay.oy + lay.pitch_h + 8
    draw.text((lay.ox, cap_y), weight_str, fill=DIM_TEXT, font=font)
    wt_w, _ = _text_size(draw, weight_str, font)
    caption_x = lay.ox + wt_w + 6
    max_caption_w = lay.img_w - caption_x - lay.ox
    lines = _wrap_text(draw, play.headline(), font, max_caption_w)
    for i, line in enumerate(lines[:3]):
        draw.text((caption_x, cap_y + i * 14), line, fill=TEXT, font=font)
    n_off = len(state.off_pitch())
    if n_off:
        draw.text((lay.ox, cap_y + len(lines) * 14 + 4),
                  f"({n_off} players off-pitch / in dugout)",
                  fill=DIM_TEXT, font=small)


def _targets_for_play(play: PivotalPlay) -> TableauTargets:
    if play.kind == "touchdown":
        return TableauTargets(scorer=play.player_id)
    if play.kind == "interception":
        return TableauTargets(scorer=play.player_id)
    return TableauTargets(victim=play.player_id, inflicter=play.inflicter_id)


def _draw_pitch(draw: ImageDraw.ImageDraw, lay: Layout) -> None:
    """Pitch base: grass, endzone tint, wide-zone lines, line of scrimmage, grid.

    Geometry differs by orientation. Vertical: endzones are top/bottom
    rows, LoS is horizontal, wide zones are vertical lines. Horizontal:
    endzones are left/right columns, LoS is vertical, wide zones are
    horizontal lines.
    """
    ox, oy, w, h = lay.ox, lay.oy, lay.pitch_w, lay.pitch_h
    draw.rectangle([ox, oy, ox + w, oy + h], fill=PITCH_GREEN)
    if lay.orientation == "vertical":
        # Endzones: top (BB x=0) and bottom (BB x=25).
        draw.rectangle([ox, oy, ox + w, oy + TILE], fill=ENDZONE_TINT)
        draw.rectangle([ox, oy + h - TILE, ox + w, oy + h], fill=ENDZONE_TINT)
        # Wide-zone separators (vertical lines at BB y=4 and y=11).
        for ywz in (4, 11):
            draw.line([ox + ywz * TILE, oy, ox + ywz * TILE, oy + h], fill=WIDE_TINT, width=1)
        # Line of scrimmage (horizontal at BB x=13).
        los = oy + 13 * TILE
        draw.line([ox, los, ox + w, los], fill=PITCH_LINE, width=2)
    else:
        # Endzones: left (BB x=0) and right (BB x=25).
        draw.rectangle([ox, oy, ox + TILE, oy + h], fill=ENDZONE_TINT)
        draw.rectangle([ox + w - TILE, oy, ox + w, oy + h], fill=ENDZONE_TINT)
        # Wide-zone separators (horizontal lines at BB y=4 and y=11).
        for ywz in (4, 11):
            draw.line([ox, oy + ywz * TILE, ox + w, oy + ywz * TILE], fill=WIDE_TINT, width=1)
        # Line of scrimmage (vertical at BB x=13).
        los = ox + 13 * TILE
        draw.line([los, oy, los, oy + h], fill=PITCH_LINE, width=2)
    # Subtle grid — independent of orientation.
    for c in range(1, lay.cols):
        gx = ox + c * TILE
        draw.line([gx, oy, gx, oy + h], fill=(48, 100, 56), width=1)
    for r in range(1, lay.rows):
        gy = oy + r * TILE
        draw.line([ox, gy, ox + w, gy], fill=(48, 100, 56), width=1)


def _draw_endzone_labels(
    img: Image.Image,
    draw: ImageDraw.ImageDraw,
    lay: Layout,
    home_name: str | None,
    away_name: str | None,
    font,
) -> None:
    """Stamp team names in the endzones. Home defends one endzone, away
    the other; in vertical layout home is on top, in horizontal layout
    home is on the left (rotated so it reads up the endzone)."""
    ox, oy = lay.ox, lay.oy
    if lay.orientation == "vertical":
        home_box = (ox, oy, lay.pitch_w, TILE)
        away_box = (ox, oy + (PITCH_WIDTH - 1) * TILE, lay.pitch_w, TILE)
    else:
        home_box = (ox, oy, TILE, lay.pitch_h)
        away_box = (ox + (PITCH_WIDTH - 1) * TILE, oy, TILE, lay.pitch_h)

    rotate = lay.orientation == "horizontal"
    if home_name:
        _draw_label_in_box(img, draw, home_name, *home_box, HOME_COLOR, font, rotate=rotate)
    if away_name:
        _draw_label_in_box(img, draw, away_name, *away_box, AWAY_COLOR, font, rotate=rotate)


def _draw_label_in_box(img, draw, text, ox, oy, w, h, color, font, *, rotate: bool):
    """Centre-render text inside a box; rotate 90° if requested."""
    if not rotate:
        tw, th = _text_size(draw, text, font)
        draw.text((ox + (w - tw) // 2, oy + (h - th) // 2), text, fill=color, font=font)
        return
    tw, th = _text_size(draw, text, font)
    tmp = Image.new("RGBA", (tw + 4, th + 4), (0, 0, 0, 0))
    ImageDraw.Draw(tmp).text((2, 2), text, fill=color, font=font)
    rotated = tmp.rotate(90, expand=True, resample=Image.BICUBIC)
    rw, rh = rotated.size
    img.alpha_composite(rotated, (ox + (w - rw) // 2, oy + (h - rh) // 2))


def _paste_logos(
    img: Image.Image,
    lay: Layout,
    home_logo: Image.Image | None,
    away_logo: Image.Image | None,
) -> None:
    """Faded team logos in each half of the pitch.

    Vertical: top half = home, bottom half = away.
    Horizontal: left half = home, right half = away.
    """
    logo_target = 5 * TILE
    if lay.orientation == "vertical":
        home_cx, home_cy = lay.ox + lay.pitch_w // 2, lay.oy + 7 * TILE
        away_cx, away_cy = lay.ox + lay.pitch_w // 2, lay.oy + 19 * TILE
    else:
        home_cx, home_cy = lay.ox + 7 * TILE, lay.oy + lay.pitch_h // 2
        away_cx, away_cy = lay.ox + 19 * TILE, lay.oy + lay.pitch_h // 2
    if home_logo is not None:
        _paste_centered_logo(img, home_logo, home_cx, home_cy, logo_target)
    if away_logo is not None:
        _paste_centered_logo(img, away_logo, away_cx, away_cy, logo_target)


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
