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


# Tile size in pixels per pitch square. Bumping this is the single
# biggest lever on output sharpness — sprites are 35-pixel native cells,
# so TILE=28 was shrinking them; TILE=56 gives them a clean 1.5x upscale
# with NEAREST and the encoded video has more pixels for h264 to keep.
TILE = 56

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
# Margins around the pitch. MARGIN_X needs to fit two-digit coord labels
# (numbers 1-12 down each side / along each edge). Scales with TILE.
MARGIN_X = 60
MARGIN_TOP = 100
CAPTION_H = 248  # caption + stats line(s) + dugout-status strip
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


_COORD_BAND = 32  # px reserved above/below the pitch for row labels (horizontal layout)


def _layout(orientation: str) -> Layout:
    if orientation == "vertical":
        cols, rows = PITCH_HEIGHT, PITCH_WIDTH       # 15 × 26
        oy = MARGIN_TOP
        extra_h = 0
    else:
        cols, rows = PITCH_WIDTH, PITCH_HEIGHT       # 26 × 15
        oy = MARGIN_TOP + _COORD_BAND                # leave room for labels above the pitch
        extra_h = 2 * _COORD_BAND                    # and below
    pitch_w = cols * TILE
    pitch_h = rows * TILE
    img_w = pitch_w + 2 * MARGIN_X
    img_h = pitch_h + MARGIN_TOP + CAPTION_H + extra_h
    return Layout(orientation, cols, rows, pitch_w, pitch_h,
                   MARGIN_X, oy, img_w, img_h)


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
    dice: list | None = None,
    pitch_background: Image.Image | None = None,
    weather: str | None = None,
    blitz_active: bool = True,        # show the crosshair on the blitz target?
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
    font = _font(26)
    small = _font(22)
    tiny = _font(18)
    endzone_font = _font(40 if orientation == "vertical" else 32)

    # Layer 1: pitch base. Use the weather-themed FFB pitch PNG when
    # we have one (full bitmap with LoS + hash marks baked in); fall
    # back to procedural drawing otherwise.
    if pitch_background is not None:
        _paste_pitch(img, lay, pitch_background)
    else:
        _draw_pitch(draw, lay)
    # Layer 2: team logo watermark (sits on the pitch but under everything else).
    _paste_logos(img, lay, home_logo, away_logo)
    # Layer 3: endzone team-name labels (drawn after logos so the text isn't washed).
    _draw_endzone_labels(img, draw, lay, home_name, away_name, endzone_font)
    # Layer 3b: row coordinate labels along the long-axis sides of the pitch.
    _draw_coord_labels(img, lay, _font(22))
    # Layer 4: header bar above the pitch.
    draw.text((lay.ox, 28), _header_text(play, weather=weather), fill=TEXT, font=font)
    # Layer 5: ball.
    _draw_ball(draw, lay, state)
    # Layer 6: players + their state markers + the highlight ring.
    _draw_players(img, draw, lay, state, player_lookup, sprites, targets, tiny, small)
    # Layer 6b: BLITZ badge on the OPPONENT that was marked against
    # (the block defender during the blitz). We only show the badge
    # when we actually know who that was — for plays where the action
    # was Blitz but no block landed (e.g. a self-kill on the GFI to
    # contact), the chip would have nowhere honest to anchor.
    if play.was_blitz and play.blitz_target_id and blitz_active:
        _draw_blitz_badge(img, draw, lay, state, play.blitz_target_id)
    # Layer 7: dice rolls that produced this play, positioned over the actor.
    if dice:
        _draw_dice(img, lay, state, dice, targets, tiny)
    # Layer 8: caption strip + per-player stats line(s) + dugout status.
    next_y = _draw_caption(draw, lay, play, state, font, small)
    _draw_stats_lines(draw, lay, play, player_lookup, small, y_start=next_y)
    _draw_dugout_strip(draw, lay, state, player_lookup,
                        home_name=home_name, away_name=away_name, font=small)

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
    sprites: dict[str, dict[str, Image.Image]],
    targets: "TableauTargets",
    tiny, small,
) -> None:
    involved = targets.involved()
    # Drawing the disc as a faint overlay is best done on an RGBA copy
    # we composite at the end of each player, since ImageDraw's `fill`
    # with alpha goes opaque on RGB-backed canvases.
    for pid, (x, y) in state.on_pitch().items():
        info = player_lookup.get(pid)
        side = info.side if info else "home"
        color = HOME_COLOR if side == "home" else AWAY_COLOR
        cx, cy = lay.bb_to_screen(x, y)
        r = TILE // 2 - 3
        sprite_pair = sprites.get(pid)
        # Match FFB: base state is the low BYTE of the bitmask; "moving"
        # sprite is used only when base == MOVING (2). The ACTIVE bit
        # (0x100) is set for every player on the team that holds the
        # turn — it's not a "currently being moved" signal.
        raw_state = state.player_states.get(pid, 0) or 0
        base_state = raw_state & 0xFF
        is_moving = base_state == _STATE_MOVING
        is_prone = base_state in _PRONE_STATES
        is_stunned = base_state == _STATE_STUNNED
        is_dead = base_state in (_STATE_KO, _STATE_BH, _STATE_SI, _STATE_RIP)

        if pid in involved:
            ring_r = r + (5 if sprite_pair else 4)
            draw.ellipse([cx - ring_r, cy - ring_r, cx + ring_r, cy + ring_r], fill=HIGHLIGHT)

        # Team-colour ring under the sprite. A thin outline (no fill)
        # avoids the muddy red+green composite the faint-disc approach
        # produced - the sprite sits cleanly inside a clear colour band.
        ring_r = r + 1
        draw.ellipse([cx - ring_r, cy - ring_r, cx + ring_r, cy + ring_r],
                     outline=color, width=2)

        if sprite_pair:
            sprite = sprite_pair["moving" if is_moving else "still"]
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


def _faint_disc(img: Image.Image, cx: int, cy: int, r: int,
                color: tuple[int, int, int], alpha: int) -> None:
    """Paint a translucent team-coloured disc onto the RGBA canvas at (cx, cy)."""
    overlay = Image.new("RGBA", (r * 2 + 2, r * 2 + 2), (0, 0, 0, 0))
    ImageDraw.Draw(overlay).ellipse([0, 0, r * 2, r * 2], fill=color + (alpha,))
    img.alpha_composite(overlay, (cx - r, cy - r))


def _draw_blitz_badge(img: Image.Image, draw: ImageDraw.ImageDraw, lay: Layout,
                       state: FieldState, target_pid: str) -> None:
    """Overlay a bright crosshair on the OPPONENT the blitzer marked
    against. Matches the visual language of an aiming reticle so the
    viewer knows at a glance which player got chosen for the blitz."""
    anchor_pid = target_pid
    if not anchor_pid:
        return
    anchor_pos = state.players.get(anchor_pid)
    if not anchor_pos:
        return
    ax, ay = anchor_pos
    if not (0 <= ax < PITCH_WIDTH and 0 <= ay < PITCH_HEIGHT):
        return
    cx, cy = lay.bb_to_screen(ax, ay)
    # Crosshair: outer ring, four tick marks with a gap before the centre,
    # inner dot. Sized to surround a player tile.
    r_outer = TILE // 2 + 4
    r_inner = TILE // 2 - 6
    width = max(2, TILE // 14)
    color = HIGHLIGHT
    # Outer ring
    draw.ellipse([cx - r_outer, cy - r_outer, cx + r_outer, cy + r_outer],
                 outline=color, width=width)
    # Tick marks (four arms) — leave a small gap before the centre dot
    tick_outer = r_outer + 6
    tick_gap = r_inner + 2
    draw.line([cx - tick_outer, cy, cx - tick_gap, cy], fill=color, width=width)
    draw.line([cx + tick_gap, cy, cx + tick_outer, cy], fill=color, width=width)
    draw.line([cx, cy - tick_outer, cx, cy - tick_gap], fill=color, width=width)
    draw.line([cx, cy + tick_gap, cx, cy + tick_outer], fill=color, width=width)
    # Centre dot
    dot = max(2, TILE // 18)
    draw.ellipse([cx - dot, cy - dot, cx + dot, cy + dot], fill=color)


def _draw_dice(img: Image.Image, lay: Layout, state: FieldState,
                dice: list, targets: "TableauTargets", font) -> None:
    """Stamp dice icons above the actor for each DiceGroup. Multiple
    groups sharing an anchor stack VERTICALLY (block on top, armor in
    the middle, injury at the bottom) so they don't overlap."""
    from . import dice as dice_mod
    if not dice:
        return
    # Bucket groups by anchor first; render each bucket as a stack.
    buckets: dict[str, list] = {}
    fallback = targets.inflicter or targets.scorer or targets.victim
    for group in dice:
        anchor_pid = group.actor_id or fallback
        if not anchor_pid:
            continue
        buckets.setdefault(anchor_pid, []).append(group)
    for anchor_pid, groups in buckets.items():
        anchor_pos = state.players.get(anchor_pid)
        if not anchor_pos:
            continue
        ax, ay = anchor_pos
        if not (0 <= ax < PITCH_WIDTH and 0 <= ay < PITCH_HEIGHT):
            continue
        cx, cy = lay.bb_to_screen(ax, ay)
        # Render each strip, top-aligned upwards from the player token.
        strips = [dice_mod.render_group_strip(g, font=font) for g in groups]
        gap = 2
        total_h = sum(s.size[1] for s in strips) + gap * (len(strips) - 1)
        max_w = max(s.size[0] for s in strips)
        # Top edge of the stack sits TILE/2+2 above the token centre.
        top_y = cy - TILE // 2 - total_h - 2
        top_y = max(lay.oy + 2, top_y)
        y = top_y
        for strip in strips:
            sw, sh = strip.size
            sx = cx - sw // 2
            sx = max(lay.ox + 2, min(sx, lay.ox + lay.pitch_w - sw - 2))
            img.alpha_composite(strip, (sx, y))
            y += sh + gap


def _draw_stats_lines(
    draw: ImageDraw.ImageDraw,
    lay: Layout,
    play: PivotalPlay,
    player_lookup: dict[str, PlayerInfo],
    font,
    *,
    y_start: int | None = None,
) -> None:
    """One short stats/skills line per featured player.

    Format per line:
      "Name (Race) — MA7 ST3 AG2+ PA3+ AV9+ — Block, Sidestep, ..."

    For TDs / interceptions / blunders we show the actor only. For
    casualties we show victim AND inflicter (two lines). Skills list
    is truncated to fit the canvas width.
    """
    ids_in_order: list[tuple[str, tuple[int, int, int]]] = []
    if play.kind == "casualty":
        if play.player_id:
            ids_in_order.append((play.player_id, AWAY_COLOR if play.team_name == play.against_team else HOME_COLOR))
        if play.inflicter_id:
            # Inflicter is on the OPPOSITE team to the victim.
            ids_in_order.append((play.inflicter_id, AWAY_COLOR))
    else:
        if play.player_id:
            ids_in_order.append((play.player_id, HOME_COLOR))

    # Resolve actor side via player_lookup so colours are right.
    cap_y = lay.oy + lay.pitch_h + (8 if lay.orientation == "vertical" else _COORD_BAND + 8)
    # Stats lines slot directly after whatever caption text was drawn.
    y = y_start if y_start is not None else cap_y + 72
    line_h = 24
    for pid, _fallback in ids_in_order:
        info = player_lookup.get(pid)
        if not info:
            continue
        color = HOME_COLOR if info.side == "home" else AWAY_COLOR
        bits = []
        if info.movement is not None: bits.append(f"MA{info.movement}")
        if info.strength is not None: bits.append(f"ST{info.strength}")
        if info.agility is not None:  bits.append(f"AG{info.agility}+")
        if info.passing is not None:  bits.append(f"PA{info.passing}+")
        if info.armour is not None:   bits.append(f"AV{info.armour}+")
        stats = " ".join(bits)
        skill_list = list(info.skills)
        skills = ", ".join(skill_list) if skill_list else "—"
        prefix = f"#{info.number or '-':<2} {info.name}  •  {stats}  •  "
        line = prefix + skills
        # Trim skills from the tail one by one until the line fits.
        max_w = lay.img_w - 2 * lay.ox
        while skill_list and _text_size(draw, line, font)[0] > max_w:
            skill_list.pop()
            skills = (", ".join(skill_list) + ", …") if skill_list else "…"
            line = prefix + skills
        draw.text((lay.ox, y), line, fill=color, font=font)
        y += line_h


def _draw_dugout_strip(
    draw: ImageDraw.ImageDraw,
    lay: Layout,
    state: FieldState,
    player_lookup: dict[str, PlayerInfo],
    *,
    home_name: str | None,
    away_name: str | None,
    font,
) -> None:
    """Show each team's off-pitch player counts at the bottom of the
    canvas: reserves / KO / BH / SI / RIP / banned. A glimpse of the
    state of the match."""
    counts = state.dugout_counts(player_lookup)
    abbrev = lambda n: (n[:14] + "…") if n and len(n) > 15 else (n or "")
    line_h = 24
    # Caption uses ~72 px, stats lines take 0-2 lines (24 px each).
    # Anchor the dugout strip near the BOTTOM of CAPTION_H so we don't
    # need to know how many stats lines were drawn above.
    y = lay.oy + lay.pitch_h + CAPTION_H - 2 * line_h - 12
    cats = ("res", "ko", "bh", "si", "rip", "ban")
    for side, color, name in (("home", HOME_COLOR, abbrev(home_name)),
                                ("away", AWAY_COLOR, abbrev(away_name))):
        bits = "  ".join(f"{k.upper()} {counts[side][k]}" for k in cats)
        label = f"{name or side.upper():<14}  {bits}"
        draw.text((lay.ox, y), label, fill=color, font=font)
        y += line_h


def _draw_caption(draw, lay: Layout, play: PivotalPlay, state: FieldState, font, small) -> int:
    """Render the [weight] + wrapped headline. Returns the y of the next free row."""
    weight_str = f"[{play.weight:.2f}]"
    # In horizontal mode the row labels occupy the band immediately below
    # the pitch, so the caption needs to start below that.
    band = _COORD_BAND if lay.orientation == "horizontal" else 0
    cap_y = lay.oy + lay.pitch_h + band + 8
    draw.text((lay.ox, cap_y), weight_str, fill=DIM_TEXT, font=font)
    wt_w, _ = _text_size(draw, weight_str, font)
    caption_x = lay.ox + wt_w + 6
    max_caption_w = lay.img_w - caption_x - lay.ox
    lines = _wrap_text(draw, play.headline(), font, max_caption_w)[:3]
    for i, line in enumerate(lines):
        draw.text((caption_x, cap_y + i * 28), line, fill=TEXT, font=font)
    return cap_y + len(lines) * 28 + 8


def _targets_for_play(play: PivotalPlay) -> TableauTargets:
    if play.kind == "touchdown":
        return TableauTargets(scorer=play.player_id)
    if play.kind == "interception":
        return TableauTargets(scorer=play.player_id)
    return TableauTargets(victim=play.player_id, inflicter=play.inflicter_id)


def _paste_pitch(img: Image.Image, lay: Layout, pitch: Image.Image) -> None:
    """Paste the FFB pitch background onto the canvas.

    The FFB PNGs are 26x15 tiles at 30px each (782x452) and horizontal
    by convention. For vertical orientation we rotate 90° clockwise so
    the long axis runs top-to-bottom. The image is then resized to
    our (pitch_w, pitch_h) so it scales cleanly with our TILE size.
    """
    if lay.orientation == "vertical":
        pitch = pitch.rotate(-90, expand=True, resample=Image.BICUBIC)
    if pitch.size != (lay.pitch_w, lay.pitch_h):
        pitch = pitch.resize((lay.pitch_w, lay.pitch_h), resample=Image.LANCZOS)
    if pitch.mode != "RGBA":
        pitch = pitch.convert("RGBA")
    img.paste(pitch, (lay.ox, lay.oy), pitch)


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
    home is on the left (rotated so it reads up the endzone). Each
    label sits on a dark opaque strip so it stays legible regardless
    of the underlying pitch texture (rain / blizzard / heat etc.)."""
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
    """Render text in a box on an opaque dark strip so it pops against
    any pitch texture. Rotate 90° if the box is taller than it is wide."""
    # Dark backing strip across the whole endzone band — nearly fully
    # opaque so the team name pops against any pitch texture.
    backing = Image.new("RGBA", (w, h), (10, 14, 16, 250))
    img.alpha_composite(backing, (ox, oy))

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


def _draw_coord_labels(img: Image.Image, lay: Layout, font) -> None:
    """Row numbers along the long axis of the pitch.

    Each half counts 1..12 from its endzone toward the line of scrimmage.
    Home labels are in HOME_COLOR (top in vertical / left in horizontal),
    away labels in AWAY_COLOR. Endzones themselves (BB x=0, 25) carry the
    team-name labels already and don't get numbered.

    Each label sits on a small dark chip so the digits stay readable
    regardless of where they fall against the canvas/pitch background.
    """
    draw = ImageDraw.Draw(img)
    for bb_x in range(1, PITCH_WIDTH - 1):
        if bb_x <= 12:
            label = str(bb_x)
            color = HOME_COLOR
        else:
            label = str(25 - bb_x)
            color = AWAY_COLOR
        tw, th = _text_size(draw, label, font)
        chip_w, chip_h = max(tw + 6, 18), th + 4
        if lay.orientation == "vertical":
            y_centre = lay.oy + bb_x * TILE + TILE // 2 - th // 2
            # Left side: chip + label, right-aligned to the pitch edge.
            _chip_label(img, lay.ox - chip_w - 2, y_centre - 2,
                        chip_w, chip_h, label, color, font, draw)
            # Right side: chip + label, left-aligned to the pitch edge.
            _chip_label(img, lay.ox + lay.pitch_w + 2, y_centre - 2,
                        chip_w, chip_h, label, color, font, draw)
        else:
            x_centre = lay.ox + bb_x * TILE + TILE // 2 - tw // 2
            _chip_label(img, x_centre - 3, lay.oy - _COORD_BAND + 1,
                        chip_w, chip_h, label, color, font, draw)
            _chip_label(img, x_centre - 3, lay.oy + lay.pitch_h + 1,
                        chip_w, chip_h, label, color, font, draw)


def _chip_label(img: Image.Image, x: int, y: int, w: int, h: int,
                 text: str, color, font, draw: ImageDraw.ImageDraw) -> None:
    # Nearly-opaque dark chip so the digit reads clearly against any pitch.
    chip = Image.new("RGBA", (w, h), (12, 16, 18, 250))
    img.alpha_composite(chip, (x, y))
    tw, _ = _text_size(draw, text, font)
    draw.text((x + (w - tw) // 2, y + 1), text, fill=color, font=font)


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


def _header_text(p: PivotalPlay, *, weather: str | None = None) -> str:
    bits = [p.team_name, "vs", p.against_team]
    if p.score_home is not None and p.score_away is not None:
        bits.append(f"  {p.score_home}-{p.score_away}")
    if p.half:
        bits.append(f"  half {p.half}")
    if p.turn:
        bits.append(f"  turn {p.turn}")
    if weather:
        bits.append(f"  •  {weather}")
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
