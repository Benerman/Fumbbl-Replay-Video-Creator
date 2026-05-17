"""Fetch and crop FUMBBL position icon sheets to get per-player sprites.

Each FUMBBL position (Elf Blitzer, Beastman Runner, etc.) has an
icon sheet at `https://fumbbl.com/i/{position.icon}`. The sheet
layout is fixed at 4 COLUMNS - these are not diversity variants but
team-side + pose pairs, the same convention the FFB Java client uses
in `PlayerIconFactory.getBasicIcon`:

  col 0 = home, standing
  col 1 = home, moving / acting
  col 2 = away, standing
  col 3 = away, moving / acting

Rows are the diversity variants within a team (each player on the
same position gets a different row so they aren't visually identical).
The per-player `positionIconIndex` from the in-game roster picks the
row; the team side picks the column.

Both fetched icons and position payloads are cached on disk under
`~/.cache/fumbbl-replay-video-creator/`. CLI runs that touch the same
match more than once will only hit FUMBBL on the first invocation.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
from PIL import Image

from .events import PlayerInfo

log = logging.getLogger(__name__)

USER_AGENT = "fumbbl-replay-video-creator/0.1"
ICON_COLS = 4

CACHE_DIR = Path(os.environ.get("FUMBBL_REPLAY_CACHE",
                                 str(Path.home() / ".cache" / "fumbbl-replay-video-creator")))


@dataclass
class PositionInfo:
    id: str
    name: str
    icon_image_id: int | None
    icon_letter: str | None


def fetch_position(position_id: str) -> PositionInfo:
    """Fetch position metadata, with on-disk caching."""
    cache_path = CACHE_DIR / "positions" / f"{position_id}.json"
    if cache_path.exists():
        data = json.loads(cache_path.read_text())
    else:
        url = f"https://fumbbl.com/api/position/get/{position_id}"
        log.info("GET %s", url)
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
        r.raise_for_status()
        data = r.json()
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(data))
    icon = data.get("icon")
    return PositionInfo(
        id=str(data.get("id") or position_id),
        name=str(data.get("name") or ""),
        icon_image_id=int(icon) if icon else None,
        icon_letter=data.get("iconLetter") or None,
    )


def fetch_icon_sheet(image_id: int) -> Image.Image:
    """Fetch a position icon sheet, cached on disk."""
    return _fetch_image(image_id, "icons")


def fetch_ffb_decoration(name: str) -> Image.Image | None:
    """Fetch an icon from the FFB Java client's `icons/` tree, cached on
    disk. `name` is a path relative to
    `ffb-resources/src/main/resources/icons/`, with or without the
    leading subdir — bare names without a slash default to
    `decorations/` (target.png, holdball.png, etc.); names like
    `game/sball_60x60` go through verbatim.

    Returns None if the asset can't be fetched.
    """
    rel = name if "/" in name else f"decorations/{name}"
    cache_path = CACHE_DIR / "ffb_decorations" / f"{rel.replace('/', '__')}.png"
    if not cache_path.exists():
        url = ("https://raw.githubusercontent.com/christerk/ffb/master/"
                f"ffb-resources/src/main/resources/icons/{rel}.png")
        try:
            log.info("GET %s", url)
            r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
            r.raise_for_status()
        except Exception as e:
            log.warning("could not fetch FFB resource %s: %s", rel, e)
            return None
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(r.content)
    try:
        return Image.open(cache_path).convert("RGBA")
    except Exception as e:
        log.warning("could not open FFB resource %s: %s", rel, e)
        return None


def fetch_team_logo(image_id: int | None) -> Image.Image | None:
    """Fetch a team logo image, cached on disk. Returns None for missing logos."""
    if not image_id:
        return None
    try:
        return _fetch_image(int(image_id), "logos")
    except Exception as e:
        log.warning("could not fetch team logo %s: %s", image_id, e)
        return None


def _fetch_image(image_id: int, subdir: str) -> Image.Image:
    cache_path = CACHE_DIR / subdir / f"{image_id}.png"
    if not cache_path.exists():
        url = f"https://fumbbl.com/i/{image_id}"
        log.info("GET %s", url)
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
        r.raise_for_status()
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(r.content)
    return Image.open(cache_path).convert("RGBA")


def extract_sprite(sheet: Image.Image, *, side: str, row: int,
                    moving: bool = False, has_ball: bool = False) -> Image.Image:
    """Crop the cell for one player from a 4-column icon sheet.

    Column layout matches FFB's `PlayerIconFactory.getBasicIcon`:
    home-still=0, home-moving=1, away-still=2, away-moving=3.

    Some sheets stack TWO rows per position — no-ball variant on
    even row, with-ball variant on odd row. When `has_ball=True`
    and the sheet is tall enough, we pick row `2P+1`. For sheets
    that don't include ball variants we fall through to the
    no-ball cell; callers can check the cell's visibility via
    `_has_visible_pixels` to decide whether to fall back to an
    overlay decoration.
    """
    w, h = sheet.size
    cell_w = w // ICON_COLS
    rows_total = max(1, h // cell_w)
    if has_ball and rows_total >= row * 2 + 2:
        effective_row = row * 2 + 1
    else:
        effective_row = max(0, min(row, rows_total - 1))
    col_base = 0 if side == "home" else 2
    col = col_base + (1 if moving else 0)
    box = (col * cell_w, effective_row * cell_w,
            (col + 1) * cell_w, (effective_row + 1) * cell_w)
    return sheet.crop(box)


def _has_visible_pixels(im: Image.Image, threshold: float = 0.05) -> bool:
    """Skip blank ball-pose cells (sheets without ball variants). Returns
    True iff at least `threshold` of the cell's pixels have meaningful
    alpha. RGBA only — non-RGBA assumed opaque."""
    if im.mode != "RGBA":
        return True
    hist = im.split()[-1].histogram()
    total = im.size[0] * im.size[1]
    if total == 0:
        return False
    return sum(hist[64:]) / total > threshold


def build_player_sprites(
    player_lookup: dict[str, PlayerInfo],
    position_icon_index: dict[str, int],
) -> dict[str, dict[str, Image.Image]]:
    """Return {playerId -> {"still": Image, "moving": Image}} for every
    player whose position we can resolve to an icon sheet. The renderer
    picks the right pose at draw time based on the player's state.
    Players without a usable sprite are omitted; the renderer falls
    back to its colour-circle drawing.

    `position_icon_index` is the per-player `positionIconIndex` from
    the in-game roster (events.roster_from_replay doesn't carry it;
    pass it in from the caller).
    """
    out: dict[str, dict[str, Image.Image]] = {}
    sheet_cache: dict[int, Image.Image] = {}
    pos_cache: dict[str, PositionInfo] = {}

    for pid, info in player_lookup.items():
        pos_id = info.position_id
        if not pos_id:
            continue
        pos = pos_cache.get(pos_id)
        if pos is None:
            try:
                pos = fetch_position(pos_id)
            except Exception as e:
                log.warning("could not fetch position %s: %s", pos_id, e)
                pos_cache[pos_id] = PositionInfo(id=pos_id, name="", icon_image_id=None, icon_letter=None)
                continue
            pos_cache[pos_id] = pos
        if not pos.icon_image_id:
            continue
        sheet = sheet_cache.get(pos.icon_image_id)
        if sheet is None:
            try:
                sheet = fetch_icon_sheet(pos.icon_image_id)
            except Exception as e:
                log.warning("could not fetch icon sheet %s: %s", pos.icon_image_id, e)
                continue
            sheet_cache[pos.icon_image_id] = sheet

        row = position_icon_index.get(pid, 0)
        variants: dict[str, Image.Image] = {
            "still":  extract_sprite(sheet, side=info.side, row=row, moving=False),
            "moving": extract_sprite(sheet, side=info.side, row=row, moving=True),
        }
        # Ball-pose pair — only attempt the lookup when the sheet
        # actually has rows 2P AND 2P+1 (some sheets stack no-ball /
        # with-ball; others don't). Without the row check we'd
        # silently store the no-ball cell as the "ball" variant and
        # the renderer would think the sprite was showing the ball
        # when it wasn't.
        cell_w_check = sheet.size[0] // ICON_COLS
        rows_total = max(1, sheet.size[1] // cell_w_check)
        if rows_total >= row * 2 + 2:
            ball_still = extract_sprite(sheet, side=info.side, row=row,
                                         moving=False, has_ball=True)
            ball_moving = extract_sprite(sheet, side=info.side, row=row,
                                          moving=True, has_ball=True)
            if _has_visible_pixels(ball_still) and _has_visible_pixels(ball_moving):
                variants["still_ball"] = ball_still
                variants["moving_ball"] = ball_moving
        out[pid] = variants
    return out


def position_icon_index_from_replay(replay: dict[str, Any]) -> dict[str, int]:
    """Pull the per-player positionIconIndex (column within icon sheet)
    out of the replay's in-game rosters - events.PlayerInfo doesn't
    carry it but the renderer needs it."""
    out: dict[str, int] = {}
    game = replay.get("game") or {}
    for side in ("Home", "Away"):
        team = game.get(f"team{side}") or {}
        for p in team.get("playerArray") or []:
            pid = str(p.get("playerId") or "")
            if pid:
                out[pid] = int(p.get("positionIconIndex") or 0)
    return out
