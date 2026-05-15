"""Identify pivotal plays from a FUMBBL match.

This is the first concrete deliverable: given the match summary JSON,
emit a ranked list of plays that mattered most.

A "pivotal play" is currently one of:

  * a touchdown   - scoring is by definition impactful
  * an injury     - BH (knocked silly for the rest of the drive),
                    SI (out of game, lingering effect on roster),
                    RIP (player dies, hardest possible swing)

We score each play by how much it moved the win-probability needle
(rough heuristic: TDs by 1.0, KILLS by 0.8, SI by 0.5, BH by 0.2). When
the event log becomes available those scores can be refined with
context like "score-tying TD in turn 16" or "casualty on a star player".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Win-probability weights. Higher = more pivotal.
_CASUALTY_WEIGHT = {"rip": 0.8, "si": 0.5, "bh": 0.2}
_TD_WEIGHT = 1.0


@dataclass
class PivotalPlay:
    kind: str          # "touchdown" | "casualty"
    detail: str        # "RIP" / "SI" / "BH" for casualties, "" for TDs
    team_id: int
    team_name: str
    against_team: str  # for color in the script
    weight: float

    def headline(self) -> str:
        if self.kind == "touchdown":
            return f"{self.team_name} scored a touchdown"
        sev = {"rip": "killed", "si": "seriously injured", "bh": "knocked out"}.get(self.detail.lower(), self.detail)
        # Casualties from the summary are attributed to the team that
        # *suffered* them - so "team X had a player killed".
        return f"{self.team_name} had a player {sev}"


@dataclass
class MatchAnalysis:
    game_id: int
    home_name: str
    away_name: str
    home_coach: str
    away_coach: str
    home_score: int
    away_score: int
    race_home: str
    race_away: str
    date: str
    division: str
    winner: str | None
    margin: int
    pivotal: list[PivotalPlay]

    def summary_line(self) -> str:
        return (
            f"#{self.game_id} ({self.date}, {self.division}) "
            f"{self.home_name} [{self.race_home}, {self.home_coach}] {self.home_score}"
            f" - "
            f"{self.away_score} {self.away_name} [{self.race_away}, {self.away_coach}]"
        )


def analyze(summary: dict[str, Any]) -> MatchAnalysis:
    home = summary["team1"]
    away = summary["team2"]

    pivotal: list[PivotalPlay] = []

    # 1. Touchdowns. Summary only tells us how many each side scored,
    #    not when or by whom. Emit one PivotalPlay per TD anyway -
    #    when the event log lands we'll replace these with timed plays.
    for _ in range(home.get("score", 0)):
        pivotal.append(PivotalPlay(
            kind="touchdown", detail="",
            team_id=home["id"], team_name=home["name"],
            against_team=away["name"], weight=_TD_WEIGHT,
        ))
    for _ in range(away.get("score", 0)):
        pivotal.append(PivotalPlay(
            kind="touchdown", detail="",
            team_id=away["id"], team_name=away["name"],
            against_team=home["name"], weight=_TD_WEIGHT,
        ))

    # 2. Casualties suffered by each team.
    for team, opp in ((home, away), (away, home)):
        cas = team.get("casualties") or {}
        for sev_key in ("rip", "si", "bh"):
            for _ in range(cas.get(sev_key, 0)):
                pivotal.append(PivotalPlay(
                    kind="casualty", detail=sev_key.upper(),
                    team_id=team["id"], team_name=team["name"],
                    against_team=opp["name"],
                    weight=_CASUALTY_WEIGHT[sev_key],
                ))

    # Highest impact first.
    pivotal.sort(key=lambda p: p.weight, reverse=True)

    home_score = int(home.get("score", 0))
    away_score = int(away.get("score", 0))
    if home_score > away_score:
        winner = home["name"]
    elif away_score > home_score:
        winner = away["name"]
    else:
        winner = None

    return MatchAnalysis(
        game_id=int(summary.get("id", 0)),
        home_name=home["name"],
        away_name=away["name"],
        home_coach=(home.get("coach") or {}).get("name", "Unknown"),
        away_coach=(away.get("coach") or {}).get("name", "Unknown"),
        home_score=home_score,
        away_score=away_score,
        race_home=(home.get("roster") or {}).get("name", "Unknown"),
        race_away=(away.get("roster") or {}).get("name", "Unknown"),
        date=summary.get("date", ""),
        division=summary.get("division", ""),
        winner=winner,
        margin=abs(home_score - away_score),
        pivotal=pivotal,
    )


def format_report(a: MatchAnalysis) -> str:
    """Human-readable terminal report."""
    lines = [
        "",
        "  " + a.summary_line(),
        "  " + "-" * len(a.summary_line()),
    ]
    if a.winner:
        lines.append(f"  Winner: {a.winner} (by {a.margin})")
    else:
        lines.append(f"  Draw, {a.home_score}-{a.away_score}")
    lines.append("")
    lines.append(f"  Pivotal plays ({len(a.pivotal)}):")
    if not a.pivotal:
        lines.append("    (no scoring or casualties recorded in summary)")
    for i, p in enumerate(a.pivotal, 1):
        lines.append(f"    {i:2d}. [{p.weight:.2f}] {p.headline()}")
    lines.append("")
    return "\n".join(lines)
