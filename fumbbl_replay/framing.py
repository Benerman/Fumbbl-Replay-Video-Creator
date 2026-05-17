"""Intro and outro slides for the highlight reel.

`render_intro_slide` produces a title card the reel opens on: team
names + races + coaches + a handful of featured players (the heaviest
hitters per roster). `render_outro_slide` produces a closing match
report — final score banner + per-team stat table (TV / cas / TD /
blocks / completions / fouls), in the same column style as FFB's
post-match Discord embed.

Both are rendered at the same canvas size as the per-play tableaux for
the chosen orientation so they stitch into the final MP4 without
resize artefacts.

`compute_match_stats` aggregates per-team totals from
`replay.game.gameResult.teamResultHome/Away.playerResults`, which the
FUMBBL match API doesn't surface but the replay JSON does. Falls back
to zeros when fields are missing.

`generate_intro_line` / `generate_outro_line` return the TTS line to
narrate each slide.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from .analyzer import MatchAnalysis, TeamInfo
from .events import PlayerInfo
from .tableau import (
    _font, _text_size,
    HOME_COLOR, AWAY_COLOR, TEXT, DIM_TEXT, HIGHLIGHT,
    Layout,
)


@dataclass
class TeamMatchStats:
    """Aggregated end-of-match totals for one team."""
    tv: int = 0                    # team value in 1000 gp
    touchdowns: int = 0
    casualties_inflicted: int = 0
    casualties_suffered: dict[str, int] = field(default_factory=lambda: {"bh": 0, "si": 0, "rip": 0})
    blocks: int = 0
    completions: int = 0
    fouls: int = 0
    interceptions: int = 0


@dataclass
class MatchStats:
    home: TeamMatchStats = field(default_factory=TeamMatchStats)
    away: TeamMatchStats = field(default_factory=TeamMatchStats)


def compute_match_stats(replay: dict[str, Any], analysis: MatchAnalysis) -> MatchStats:
    """Pull per-team totals from replay.game.gameResult.playerResults.

    The match API only exposes score + casualties-suffered. Blocks /
    fouls / completions etc. are on the per-player records inside the
    replay JSON. We sum across each side's playerResults.
    """
    stats = MatchStats()
    stats.home.tv = (analysis.home.team_value or 0) // 1000
    stats.away.tv = (analysis.away.team_value or 0) // 1000
    stats.home.touchdowns = analysis.home.score
    stats.away.touchdowns = analysis.away.score
    stats.home.casualties_suffered = dict(analysis.home.casualties)
    stats.away.casualties_suffered = dict(analysis.away.casualties)
    game = replay.get("game", {}) or {}
    game_result = game.get("gameResult") or {}
    for side, dst in (("Home", stats.home), ("Away", stats.away)):
        team_result = game_result.get(f"teamResult{side}") or {}
        for pr in team_result.get("playerResults") or []:
            dst.blocks += int(pr.get("blocks") or 0)
            dst.completions += int(pr.get("completions") or 0)
            dst.fouls += int(pr.get("fouls") or 0)
            dst.interceptions += int(pr.get("interceptions") or 0)
            dst.casualties_inflicted += int(pr.get("casualties") or 0)
    return stats


def _key_players(player_lookup: dict[str, PlayerInfo], side: str, n: int = 3) -> list[PlayerInfo]:
    """Pick a few featured players per team. Sort by skill count desc
    (proxy for star value), then by ST + AV desc."""
    candidates = [p for p in player_lookup.values() if p.side == side]

    def score(p: PlayerInfo) -> tuple:
        return (
            -len(p.skills or []),
            -(p.strength or 0),
            -(p.armour or 0),
            p.name,
        )

    candidates.sort(key=score)
    return candidates[:n]


def _bg_canvas(orientation: str) -> tuple[Image.Image, ImageDraw.ImageDraw, int, int]:
    """Black canvas at the same dimensions as the tableau for this
    orientation. We don't need a full Layout — slides are static, not
    pitch-aligned."""
    # Match the tableau dimensions so the slide concats cleanly.
    if orientation == "vertical":
        w, h = 960, 1804
    else:
        w, h = 1576, 1252
    img = Image.new("RGBA", (w, h), (10, 14, 18, 255))
    draw = ImageDraw.Draw(img)
    return img, draw, w, h


def render_intro_slide(
    analysis: MatchAnalysis,
    player_lookup: dict[str, PlayerInfo],
    out_path: Path,
    *,
    orientation: str = "vertical",
    home_logo: Image.Image | None = None,
    away_logo: Image.Image | None = None,
) -> Path:
    """Title card opening the reel: home vs away, races, coaches, and
    three featured players per team."""
    img, draw, w, h = _bg_canvas(orientation)
    title_font = _font(72)
    big_font = _font(56)
    name_font = _font(36)
    info_font = _font(28)
    small_font = _font(22)

    # Top: small caption with division / date / week if available.
    cap_parts = []
    if analysis.division:
        cap_parts.append(analysis.division)
    if analysis.date:
        cap_parts.append(analysis.date)
    if cap_parts:
        cap = "  •  ".join(cap_parts)
        cw, _ = _text_size(draw, cap, info_font)
        draw.text(((w - cw) // 2, 40), cap, fill=DIM_TEXT, font=info_font)

    # Big "MATCH HIGHLIGHTS" line.
    title = "MATCH HIGHLIGHTS"
    tw, _ = _text_size(draw, title, title_font)
    draw.text(((w - tw) // 2, 100), title, fill=TEXT, font=title_font)

    # Team panels.
    if orientation == "vertical":
        _draw_team_panel(img, draw, analysis.home, player_lookup, "home",
                          home_logo, name_font, info_font, small_font,
                          box=(40, 240, w - 40, (h // 2) - 40))
        # VS divider
        vsw, _ = _text_size(draw, "VS", big_font)
        draw.text(((w - vsw) // 2, h // 2 - 30), "VS", fill=HIGHLIGHT, font=big_font)
        _draw_team_panel(img, draw, analysis.away, player_lookup, "away",
                          away_logo, name_font, info_font, small_font,
                          box=(40, (h // 2) + 60, w - 40, h - 80))
    else:
        # Horizontal: side-by-side.
        mid_x = w // 2
        _draw_team_panel(img, draw, analysis.home, player_lookup, "home",
                          home_logo, name_font, info_font, small_font,
                          box=(40, 220, mid_x - 60, h - 80))
        vsw, _ = _text_size(draw, "VS", big_font)
        draw.text((mid_x - vsw // 2, h // 2 - 30), "VS", fill=HIGHLIGHT, font=big_font)
        _draw_team_panel(img, draw, analysis.away, player_lookup, "away",
                          away_logo, name_font, info_font, small_font,
                          box=(mid_x + 60, 220, w - 40, h - 80))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.convert("RGB").save(out_path)
    return out_path


def _draw_team_panel(
    img: Image.Image,
    draw: ImageDraw.ImageDraw,
    team: TeamInfo,
    player_lookup: dict[str, PlayerInfo],
    side: str,
    logo: Image.Image | None,
    name_font, info_font, small_font,
    *,
    box: tuple[int, int, int, int],
) -> None:
    """One team's intro panel — logo + name + race / coach + 3 stars."""
    x0, y0, x1, y1 = box
    color = HOME_COLOR if side == "home" else AWAY_COLOR

    cur_y = y0

    if logo is not None:
        logo_dim = 180
        logo_resized = logo.resize((logo_dim, logo_dim), resample=Image.LANCZOS)
        logo_x = x0 + ((x1 - x0) - logo_dim) // 2
        img.alpha_composite(logo_resized, (logo_x, cur_y))
        cur_y += logo_dim + 16

    # Team name (centered).
    name = team.name
    nw, nh = _text_size(draw, name, name_font)
    # Wrap if too wide.
    if nw > (x1 - x0) - 20:
        # Truncate with ellipsis.
        while name and _text_size(draw, name + "…", name_font)[0] > (x1 - x0) - 20:
            name = name[:-1]
        name = name + "…"
        nw, nh = _text_size(draw, name, name_font)
    draw.text((x0 + ((x1 - x0) - nw) // 2, cur_y), name, fill=color, font=name_font)
    cur_y += nh + 12

    # Race + coach.
    info = f"{team.race}  •  Coach {team.coach}"
    iw, ih = _text_size(draw, info, info_font)
    draw.text((x0 + ((x1 - x0) - iw) // 2, cur_y), info, fill=DIM_TEXT, font=info_font)
    cur_y += ih + 20

    # Featured players.
    label = "Featured players"
    lw, lh = _text_size(draw, label, info_font)
    draw.text((x0 + ((x1 - x0) - lw) // 2, cur_y), label, fill=TEXT, font=info_font)
    cur_y += lh + 8

    stars = _key_players(player_lookup, side, n=3)
    for p in stars:
        bits = []
        if p.movement is not None: bits.append(f"MA{p.movement}")
        if p.strength is not None: bits.append(f"ST{p.strength}")
        if p.agility is not None:  bits.append(f"AG{p.agility}+")
        if p.passing is not None:  bits.append(f"PA{p.passing}+")
        if p.armour is not None:   bits.append(f"AV{p.armour}+")
        stats = " ".join(bits)
        skills = ", ".join(p.skills or [])
        # Truncate skills tail.
        line = f"#{p.number or '-':<2} {p.name}  •  {stats}  •  {skills or '—'}"
        max_w = (x1 - x0) - 20
        skill_list = list(p.skills or [])
        while skill_list and _text_size(draw, line, small_font)[0] > max_w:
            skill_list.pop()
            skills = (", ".join(skill_list) + ", …") if skill_list else "…"
            line = f"#{p.number or '-':<2} {p.name}  •  {stats}  •  {skills}"
        lw, lh2 = _text_size(draw, line, small_font)
        draw.text((x0 + ((x1 - x0) - lw) // 2, cur_y), line, fill=color, font=small_font)
        cur_y += lh2 + 8


def render_outro_slide(
    analysis: MatchAnalysis,
    stats: MatchStats,
    out_path: Path,
    *,
    orientation: str = "vertical",
    home_logo: Image.Image | None = None,
    away_logo: Image.Image | None = None,
) -> Path:
    """Closing match-report card: final score banner + per-team stat
    table (TV / Cas / T / B / C / F). Layout mirrors the FFB Discord
    embed style."""
    img, draw, w, h = _bg_canvas(orientation)
    title_font = _font(72)
    score_font = _font(120)
    name_font = _font(32)
    cell_font = _font(28)
    head_font = _font(26)

    # Header.
    title = "MATCH REPORT"
    tw, _ = _text_size(draw, title, title_font)
    draw.text(((w - tw) // 2, 60), title, fill=TEXT, font=title_font)

    # Score banner.
    home_score = analysis.home.score
    away_score = analysis.away.score
    banner = f"{home_score}  -  {away_score}"
    bw, bh = _text_size(draw, banner, score_font)
    banner_y = 200
    draw.text(((w - bw) // 2, banner_y), banner, fill=HIGHLIGHT, font=score_font)

    # Result line.
    if home_score == away_score:
        verdict = "Draw"
    elif home_score > away_score:
        verdict = f"{analysis.home.name} take the win"
    else:
        verdict = f"{analysis.away.name} take the win"
    vw, vh = _text_size(draw, verdict, name_font)
    draw.text(((w - vw) // 2, banner_y + bh + 20), verdict, fill=TEXT, font=name_font)

    # Stat table.
    cols = ("Team", "TV", "Cas", "TD", "B", "C", "F")
    rows = [
        ("home", analysis.home, stats.home),
        ("away", analysis.away, stats.away),
    ]
    table_top = banner_y + bh + 100
    table_left = 40
    table_right = w - 40
    table_width = table_right - table_left
    # Column widths: team is ~45% of the table; the rest split evenly.
    team_col_w = int(table_width * 0.45)
    stat_col_w = (table_width - team_col_w) // (len(cols) - 1)
    col_xs = [table_left, table_left + team_col_w]
    for i in range(2, len(cols)):
        col_xs.append(col_xs[-1] + stat_col_w)

    # Header row.
    row_h = 64
    y = table_top
    for col, x in zip(cols, col_xs):
        anchor_x = x + 6 if col == "Team" else x + stat_col_w // 2
        cw, _ = _text_size(draw, col, head_font)
        if col == "Team":
            draw.text((anchor_x, y + 10), col, fill=DIM_TEXT, font=head_font)
        else:
            draw.text((anchor_x - cw // 2, y + 10), col, fill=DIM_TEXT, font=head_font)
    y += row_h
    # Divider line.
    draw.rectangle([table_left, y - 4, table_right, y - 2], fill=(60, 70, 80))

    for side, team, sdata in rows:
        color = HOME_COLOR if side == "home" else AWAY_COLOR
        cas = sdata.casualties_suffered
        cas_str = f"{cas.get('bh', 0)}/{cas.get('si', 0)}/{cas.get('rip', 0)}"
        values = [
            team.name,
            f"{sdata.tv}k",
            cas_str,
            str(sdata.touchdowns),
            str(sdata.blocks),
            str(sdata.completions),
            str(sdata.fouls),
        ]
        for col, x, val in zip(cols, col_xs, values):
            if col == "Team":
                name = val
                while name and _text_size(draw, name, cell_font)[0] > team_col_w - 12:
                    name = name[:-1]
                if name != val:
                    name = name + "…"
                draw.text((x + 6, y + 14), name, fill=color, font=cell_font)
            else:
                cw, _ = _text_size(draw, val, cell_font)
                draw.text((x + stat_col_w // 2 - cw // 2, y + 14), val, fill=color, font=cell_font)
        y += row_h

    # Footer caption.
    footer = "BH / SI / RIP   •   TD: touchdowns   •   B: blocks   •   C: completions   •   F: fouls"
    fw, fh = _text_size(draw, footer, head_font)
    draw.text(((w - fw) // 2, h - 80), footer, fill=DIM_TEXT, font=head_font)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.convert("RGB").save(out_path)
    return out_path


def generate_intro_line(analysis: MatchAnalysis, player_lookup: dict[str, PlayerInfo]) -> str:
    """One short TTS line that introduces the matchup."""
    home_stars = _key_players(player_lookup, "home", n=1)
    away_stars = _key_players(player_lookup, "away", n=1)
    star_bit = ""
    if home_stars and away_stars:
        star_bit = (f" Watch out for {home_stars[0].name} on {analysis.home.name},"
                     f" and {away_stars[0].name} on {analysis.away.name}.")
    return (f"Welcome to the highlights of {analysis.home.name},"
             f" versus {analysis.away.name}.{star_bit}")


def generate_outro_line(analysis: MatchAnalysis) -> str:
    """One short TTS line that summarises the final result."""
    h, a = analysis.home.score, analysis.away.score
    if h == a:
        return f"The final whistle blows. {analysis.home.name} {h}, {analysis.away.name} {a}. A draw."
    winner = analysis.home.name if h > a else analysis.away.name
    return f"Final score, {analysis.home.name} {h}, to {analysis.away.name} {a}. {winner} take the win."
