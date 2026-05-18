"""Build YouTube title / description / tags from the analyser output.

Each match produces TWO uploads (regular 16:9 + Shorts 9:16), with
slightly different metadata:

- Regular: descriptive title, full description, no Shorts tagging.
- Short:   title shorter (Shorts feed cuts off long titles) with a
           '#Shorts' hashtag; description leads with the hashtag so
           YouTube reliably classifies the video into the Shorts feed.

Both descriptions include the Games Workshop IP disclaimer per
GW's fan-content policy.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

Variant = Literal["regular", "short"]

# Block included in every description. Keep at the bottom so the
# fold-out region of YouTube's UI shows our match content first.
GW_DISCLAIMER = (
    "Blood Bowl and all related characters, logos, and intellectual "
    "property are © Games Workshop Ltd. This channel is unofficial fan "
    "content; no endorsement or affiliation with Games Workshop is "
    "implied. Match data and replays courtesy of FUMBBL (fumbbl.com)."
)

# Shared tag floor. Per-match tags (races, divisions) are appended.
BASE_TAGS = [
    "Blood Bowl",
    "BloodBowl",
    "FUMBBL",
    "Tabletop",
    "Warhammer",
    "Games Workshop",
    "Match Highlights",
    "Fantasy Football",
]

SHORTS_TAGS = ["Shorts", "BloodBowlShorts", "FUMBBLShorts"]


@dataclass
class VideoMetadata:
    title: str
    description: str
    tags: list[str]


def build(
    analysis: dict[str, Any],
    match_id: int | None,
    replay_id: int | None,
    *,
    variant: Variant = "regular",
) -> VideoMetadata:
    """Returns title (<=100 chars per YT limit), description, tags."""
    home = (analysis.get("home") or {}).get("name", "Home")
    away = (analysis.get("away") or {}).get("name", "Away")
    h_score = (analysis.get("home") or {}).get("score", 0)
    a_score = (analysis.get("away") or {}).get("score", 0)
    h_race = (analysis.get("home") or {}).get("race") or ""
    a_race = (analysis.get("away") or {}).get("race") or ""
    division = analysis.get("division") or ""
    date = analysis.get("date") or ""
    mid = match_id or analysis.get("match_id") or 0

    title = _build_title(variant, home, away, h_score, a_score, mid)
    description = _build_description(
        variant,
        home=home, away=away, h_score=h_score, a_score=a_score,
        h_race=h_race, a_race=a_race,
        division=division, date=date, mid=mid,
        plays=analysis.get("pivotal") or [],
        h_cas=(analysis.get("home") or {}).get("casualties") or {},
        a_cas=(analysis.get("away") or {}).get("casualties") or {},
    )
    tags = list(BASE_TAGS)
    if variant == "short":
        tags = SHORTS_TAGS + tags
    for race in (h_race, a_race):
        if race and race not in tags:
            tags.append(race)
    return VideoMetadata(title=title, description=description, tags=tags)


def _build_title(
    variant: Variant,
    home: str, away: str,
    h_score: int, a_score: int,
    mid: int,
) -> str:
    if variant == "short":
        # Shorts feed truncates long titles aggressively; keep punchy.
        # '#Shorts' in the title is the strongest signal to YT's
        # classifier alongside vertical aspect + duration.
        return _truncate(f"{home} {h_score}-{a_score} {away} #Shorts #BloodBowl", 100)
    return _truncate(f"{home} {h_score}-{a_score} {away} — Match {mid}", 100)


def _build_description(
    variant: Variant,
    *,
    home: str, away: str,
    h_score: int, a_score: int,
    h_race: str, a_race: str,
    division: str, date: str, mid: int,
    plays: list[dict[str, Any]],
    h_cas: dict[str, Any],
    a_cas: dict[str, Any],
) -> str:
    lines: list[str] = []

    if variant == "short":
        # Hashtags up top — YouTube uses the first few words of the
        # description as additional signal for Shorts classification.
        lines.append("#Shorts #BloodBowl #FUMBBL")
        lines.append("")

    lines.append(f"FUMBBL match: https://fumbbl.com/p/match?id={mid}")
    head = f"{division}  •  {date}".strip(" •")
    if head:
        lines.append(head)

    lines.append("")
    matchup = f"{home}"
    if h_race:
        matchup += f" ({h_race})"
    matchup += f" {h_score} — {a_score} {away}"
    if a_race:
        matchup += f" ({a_race})"
    lines.append(matchup)

    if any(h_cas.values()) or any(a_cas.values()):
        lines.append("")
        lines.append("Casualties suffered (BH/SI/RIP):")
        lines.append(
            f"  {home}: {h_cas.get('bh', 0)}/{h_cas.get('si', 0)}/{h_cas.get('rip', 0)}"
        )
        lines.append(
            f"  {away}: {a_cas.get('bh', 0)}/{a_cas.get('si', 0)}/{a_cas.get('rip', 0)}"
        )

    if plays and variant == "regular":
        # Pivotal-plays list is too long for Shorts descriptions and
        # gets buried by the UI overlays anyway.
        lines.append("")
        lines.append("Pivotal plays:")
        for i, p in enumerate(plays[:20], 1):
            line = _format_play_line(p)
            if line:
                lines.append(f"  {i}. {line}")

    lines.append("")
    lines.append("Rendered automatically by fumbbl-replay-video-creator.")
    lines.append("")
    lines.append(GW_DISCLAIMER)

    description = "\n".join(lines)
    if len(description) > 4500:
        description = description[:4500] + "…"
    return description


def _format_play_line(p: dict[str, Any]) -> str:
    """Pull a human one-liner out of a pivotal-play dict."""
    headline = p.get("headline")
    if headline:
        return str(headline)
    kind = p.get("kind", "")
    name = p.get("player_name") or "a player"
    team = p.get("team_name") or "?"
    if kind == "touchdown":
        return f"{name} ({team}) scored a touchdown"
    if kind == "casualty":
        sev = (p.get("detail") or "").upper()
        return f"{name} ({team}) was {sev} casualty"
    if kind in ("double_skull", "triple_skull"):
        return f"{name} ({team}) rolled snake-eyes"
    return f"{kind}: {name} ({team})"


def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"
