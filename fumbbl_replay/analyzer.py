"""Identify pivotal plays from a FUMBBL match.

Given a match summary (and optionally the two team rosters), emit a
ranked list of plays that mattered most plus the assets we know we
have to draw with: team logos and player portraits.

A "pivotal play" is currently one of:

  * a touchdown   - scoring is by definition impactful
  * an injury     - BH (knocked out, lingering effect on drive),
                    SI (out of game, roster-level impact),
                    RIP (dead, hardest possible swing)

Each play is weighted by rough win-probability impact:
  TD 1.0, RIP 0.8, SI 0.5, BH 0.2.
When the per-turn event log becomes available we can refine with
context like "score-tying TD in the last turn" or "casualty on a
star player".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .fumbbl_api import image_url


_CASUALTY_WEIGHT = {"rip": 0.8, "si": 0.5, "bh": 0.2}
_TD_WEIGHT = 1.0


@dataclass
class PivotalPlay:
    kind: str          # "touchdown" | "casualty"
    detail: str        # "RIP" / "SI" / "BH" for casualties, "" for TDs
    team_id: int
    team_name: str
    against_team: str
    weight: float

    def headline(self) -> str:
        if self.kind == "touchdown":
            return f"{self.team_name} scored a touchdown"
        sev = {"rip": "killed", "si": "seriously injured", "bh": "knocked out"}.get(
            self.detail.lower(), self.detail
        )
        return f"{self.team_name} had a player {sev}"


@dataclass
class TeamInfo:
    id: int
    name: str
    race: str
    coach: str
    score: int
    team_value: int
    logo_url: str | None
    casualties: dict[str, int]
    players: list[dict[str, Any]] = field(default_factory=list)

    @property
    def player_count(self) -> int:
        return len(self.players)


@dataclass
class MatchAnalysis:
    match_id: int
    replay_id: int
    date: str
    division: str
    home: TeamInfo
    away: TeamInfo
    winner: str | None
    margin: int
    pivotal: list[PivotalPlay]

    def summary_line(self) -> str:
        return (
            f"#{self.match_id} ({self.date}, {self.division}) "
            f"{self.home.name} [{self.home.race}, {self.home.coach}] {self.home.score}"
            f" - "
            f"{self.away.score} {self.away.name} [{self.away.race}, {self.away.coach}]"
        )


def analyze(
    summary: dict[str, Any],
    team_home: dict[str, Any] | None = None,
    team_away: dict[str, Any] | None = None,
) -> MatchAnalysis:
    home_raw = summary["team1"]
    away_raw = summary["team2"]
    home = _team_info(home_raw, team_home)
    away = _team_info(away_raw, team_away)

    pivotal: list[PivotalPlay] = []
    for _ in range(home.score):
        pivotal.append(_td(home, away))
    for _ in range(away.score):
        pivotal.append(_td(away, home))

    for team, opp in ((home, away), (away, home)):
        for sev in ("rip", "si", "bh"):
            for _ in range(team.casualties.get(sev, 0)):
                pivotal.append(PivotalPlay(
                    kind="casualty", detail=sev.upper(),
                    team_id=team.id, team_name=team.name,
                    against_team=opp.name,
                    weight=_CASUALTY_WEIGHT[sev],
                ))

    pivotal.sort(key=lambda p: p.weight, reverse=True)

    if home.score > away.score:
        winner = home.name
    elif away.score > home.score:
        winner = away.name
    else:
        winner = None

    return MatchAnalysis(
        match_id=int(summary.get("id", 0)),
        replay_id=int(summary.get("replayId", 0) or 0),
        date=summary.get("date", ""),
        division=summary.get("division", ""),
        home=home,
        away=away,
        winner=winner,
        margin=abs(home.score - away.score),
        pivotal=pivotal,
    )


def _team_info(match_team: dict[str, Any], full: dict[str, Any] | None) -> TeamInfo:
    coach = match_team.get("coach") or {}
    if isinstance(coach, dict):
        coach_name = coach.get("name", "Unknown")
    else:
        coach_name = str(coach)
    roster = match_team.get("roster")
    race = roster.get("name") if isinstance(roster, dict) else (roster or "Unknown")

    logo_id = None
    players: list[dict[str, Any]] = []
    if full:
        bio = full.get("bio") or {}
        logo_id = bio.get("image") or full.get("logo")
        for p in full.get("players") or []:
            players.append({
                "id": p.get("id"),
                "name": p.get("name"),
                "number": p.get("number"),
                "position": p.get("position"),
                "skills": p.get("skills") or [],
                "portrait_url": image_url(p.get("portrait")),
                "injuries": p.get("injuries") or "",
            })

    return TeamInfo(
        id=int(match_team.get("id", 0)),
        name=match_team.get("name", "Unknown"),
        race=race,
        coach=coach_name,
        score=int(match_team.get("score", 0)),
        team_value=int(match_team.get("teamValue", 0)),
        logo_url=image_url(logo_id),
        casualties=dict(match_team.get("casualties") or {}),
        players=players,
    )


def _td(team: TeamInfo, opp: TeamInfo) -> PivotalPlay:
    return PivotalPlay(
        kind="touchdown", detail="",
        team_id=team.id, team_name=team.name, against_team=opp.name,
        weight=_TD_WEIGHT,
    )


def format_report(a: MatchAnalysis) -> str:
    lines = [
        "",
        "  " + a.summary_line(),
        "  " + "-" * len(a.summary_line()),
    ]
    if a.winner:
        lines.append(f"  Winner: {a.winner} (by {a.margin})")
    else:
        lines.append(f"  Draw, {a.home.score}-{a.away.score}")

    for t in (a.home, a.away):
        lines.append("")
        lines.append(f"  {t.name} ({t.race}, coach {t.coach}) - TV {t.team_value//1000}k")
        if t.logo_url:
            lines.append(f"     logo: {t.logo_url}")
        if t.players:
            lines.append(f"     roster: {t.player_count} players")
        lines.append(f"     casualties suffered: BH={t.casualties.get('bh',0)} SI={t.casualties.get('si',0)} RIP={t.casualties.get('rip',0)}")

    lines.append("")
    lines.append(f"  Pivotal plays ({len(a.pivotal)}):")
    if not a.pivotal:
        lines.append("    (no scoring or casualties recorded)")
    for i, p in enumerate(a.pivotal, 1):
        lines.append(f"    {i:2d}. [{p.weight:.2f}] {p.headline()}")
    lines.append("")
    return "\n".join(lines)
