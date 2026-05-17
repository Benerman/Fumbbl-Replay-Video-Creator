"""Build YouTube title / description / tags from the analyser output."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class VideoMetadata:
    title: str
    description: str
    tags: list[str]


def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def build(analysis: dict[str, Any], match_id: int | None, replay_id: int | None) -> VideoMetadata:
    """Returns title (<=100 chars per YT limit), description, tags.

    Falls back gracefully when the analyser dict is missing fields
    (e.g. when the second-pass JSON capture failed).
    """
    home = (analysis.get("home") or {}).get("name", "Home")
    away = (analysis.get("away") or {}).get("name", "Away")
    h_score = (analysis.get("home") or {}).get("score", 0)
    a_score = (analysis.get("away") or {}).get("score", 0)
    division = analysis.get("division") or ""
    date = analysis.get("date") or ""
    mid = match_id or analysis.get("match_id") or 0

    title = _truncate(f"{home} {h_score}-{a_score} {away} — Match {mid}", 100)

    desc_lines: list[str] = [f"FUMBBL match: https://fumbbl.com/p/match?id={mid}"]
    if division or date:
        desc_lines.append(f"{division}  •  {date}".strip(" •"))
    desc_lines.append("")
    desc_lines.append(f"Final score: {home} {h_score} — {away} {a_score}")

    h_cas = (analysis.get("home") or {}).get("casualties") or {}
    a_cas = (analysis.get("away") or {}).get("casualties") or {}
    if any(h_cas.values()) or any(a_cas.values()):
        desc_lines.append("")
        desc_lines.append("Casualties suffered (BH/SI/RIP):")
        desc_lines.append(
            f"  {home}: {h_cas.get('bh', 0)}/{h_cas.get('si', 0)}/{h_cas.get('rip', 0)}"
        )
        desc_lines.append(
            f"  {away}: {a_cas.get('bh', 0)}/{a_cas.get('si', 0)}/{a_cas.get('rip', 0)}"
        )

    plays = analysis.get("pivotal") or []
    if plays:
        desc_lines.append("")
        desc_lines.append("Pivotal plays:")
        for i, p in enumerate(plays[:20], 1):
            line = _format_play_line(p)
            if line:
                desc_lines.append(f"  {i}. {line}")

    desc_lines.append("")
    desc_lines.append("Rendered by fumbbl-replay-video-creator.")

    description = "\n".join(desc_lines)
    # YouTube allows up to 5000 chars; keep well under.
    if len(description) > 4500:
        description = description[:4500] + "…"

    tags = ["Blood Bowl", "FUMBBL", "Match Highlights"]
    for side in ("home", "away"):
        race = (analysis.get(side) or {}).get("race")
        if race and race not in tags:
            tags.append(race)
    return VideoMetadata(title=title, description=description, tags=tags)


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
