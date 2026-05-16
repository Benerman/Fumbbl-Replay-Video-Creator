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


def extract_sprite(sheet: Image.Image, *, side: str, row: int, moving: bool = False) -> Image.Image:
    """Crop the cell for one player from a 4-column icon sheet.

    Column layout matches FFB's `PlayerIconFactory.getBasicIcon`:
    home-still=0, home-moving=1, away-still=2, away-moving=3.
    """
    w, h = sheet.size
    cell_w = w // ICON_COLS
    rows_total = max(1, h // cell_w)
    row = max(0, min(row, rows_total - 1))
    col_base = 0 if side == "home" else 2
    col = col_base + (1 if moving else 0)
    box = (col * cell_w, row * cell_w, (col + 1) * cell_w, (row + 1) * cell_w)
    return sheet.crop(box)


def build_player_sprites(
    player_lookup: dict[str, PlayerInfo],
    position_icon_index: dict[str, int],
) -> dict[str, Image.Image]:
    """Return {playerId -> sprite Image} for every player whose position
    we can resolve to an icon sheet. Players without a usable sprite are
    omitted; the renderer falls back to its colour-circle drawing.

    `position_icon_index` is the per-player `positionIconIndex` from the
    in-game roster (events.roster_from_replay doesn't carry it; pass it
    in from the caller).
    """
    out: dict[str, Image.Image] = {}
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
            except Exception as e:  # network / 404 / unknown position
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
        out[pid] = extract_sprite(sheet, side=info.side, row=row, moving=False)
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
