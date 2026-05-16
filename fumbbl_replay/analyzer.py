"""Identify pivotal plays from a FUMBBL match.

Given a match summary, optionally enriched with the two team rosters
and the per-event replay timeline, emit a ranked list of plays that
mattered most.

A pivotal play is one of:

  * a touchdown   - scoring is by definition impactful
  * an injury     - BH (KO; lingering drive-level impact),
                    SI (out of game, roster-level impact),
                    RIP (dead, hardest possible swing)

Weights:  TD 1.0  RIP 0.8  SI 0.5  BH 0.2.

When events from the replay log are supplied the report names the
scoring/injured player, the half, and the turn. Without events we
fall back to summary totals only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .events import Event, PlayerInfo
from .fumbbl_api import image_url


_CASUALTY_WEIGHT = {"rip": 0.8, "si": 0.5, "bh": 0.2}
_TD_WEIGHT = 1.0


@dataclass
class PivotalPlay:
    kind: str          # "touchdown" | "casualty"
    detail: str        # "RIP" / "SI" / "BH" for casualties; "" for TDs
    team_id: int
    team_name: str
    against_team: str
    weight: float
    half: int | None = None
    turn: int | None = None
    player_id: str | None = None
    player_name: str | None = None
    injury_label: str | None = None

    def headline(self) -> str:
        actor = self.player_name or "a player"
        when = self._when_phrase()
        if self.kind == "touchdown":
            if self.player_name:
                return f"{self.player_name} ({self.team_name}) scored a touchdown{when}"
            return f"{self.team_name} scored a touchdown{when}"
        sev = {"rip": "killed", "si": "seriously injured", "bh": "knocked out"}.get(
            self.detail.lower(), self.detail
        )
        label = f" - {self.injury_label}" if self.injury_label and self.detail.lower() != "rip" else ""
        if self.player_name:
            return f"{self.player_name} ({self.team_name}) was {sev}{when}{label}"
        return f"{self.team_name} had {actor} {sev}{when}{label}"

    def _when_phrase(self) -> str:
        if not self.half and not self.turn:
            return ""
        parts = []
        if self.turn:
            parts.append(f"turn {self.turn}")
        if self.half:
            parts.append(f"half {self.half}")
        return " (" + ", ".join(parts) + ")" if parts else ""


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

    def player_name(self, player_id: str | None) -> str | None:
        if not player_id:
            return None
        for p in self.players:
            if str(p.get("id")) == str(player_id):
                return p.get("name")
        return None


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
    has_event_log: bool = False

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
    events: list[Event] | None = None,
    player_lookup: dict[str, PlayerInfo] | None = None,
) -> MatchAnalysis:
    home_raw = summary["team1"]
    away_raw = summary["team2"]
    home = _team_info(home_raw, team_home)
    away = _team_info(away_raw, team_away)

    if events:
        pivotal = _pivotal_from_events(events, home, away, player_lookup or {})
        has_event_log = True
    else:
        pivotal = _pivotal_from_summary(home, away)
        has_event_log = False

    pivotal.sort(key=lambda p: (-p.weight, p.half or 0, p.turn or 0, p.team_name))

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
        has_event_log=has_event_log,
    )


def _pivotal_from_events(
    events: list[Event],
    home: TeamInfo,
    away: TeamInfo,
    player_lookup: dict[str, PlayerInfo],
) -> list[PivotalPlay]:
    def resolve_name(team: TeamInfo, player_id: str | None) -> str | None:
        if not player_id:
            return None
        info = player_lookup.get(str(player_id))
        if info and info.name:
            return info.name
        # Fall back to the persistent roster (rare; replay roster nearly always wins).
        return team.player_name(player_id)

    out: list[PivotalPlay] = []
    for e in events:
        team = home if e.side == "home" else away
        opp = away if e.side == "home" else home
        if e.kind == "touchdown":
            out.append(PivotalPlay(
                kind="touchdown", detail="",
                team_id=team.id, team_name=team.name, against_team=opp.name,
                weight=_TD_WEIGHT,
                half=e.half or None, turn=e.turn or None,
                player_id=e.player_id,
                player_name=resolve_name(team, e.player_id),
            ))
        elif e.kind in ("kill", "serious_injury", "badly_hurt"):
            sev = {"kill": "rip", "serious_injury": "si", "badly_hurt": "bh"}[e.kind]
            out.append(PivotalPlay(
                kind="casualty", detail=sev.upper(),
                team_id=team.id, team_name=team.name, against_team=opp.name,
                weight=_CASUALTY_WEIGHT[sev],
                half=e.half or None, turn=e.turn or None,
                player_id=e.player_id,
                player_name=resolve_name(team, e.player_id),
                injury_label=e.detail,
            ))
    return out


def _pivotal_from_summary(home: TeamInfo, away: TeamInfo) -> list[PivotalPlay]:
    out: list[PivotalPlay] = []
    for _ in range(home.score):
        out.append(_td(home, away))
    for _ in range(away.score):
        out.append(_td(away, home))
    for team, opp in ((home, away), (away, home)):
        for sev in ("rip", "si", "bh"):
            for _ in range(team.casualties.get(sev, 0)):
                out.append(PivotalPlay(
                    kind="casualty", detail=sev.upper(),
                    team_id=team.id, team_name=team.name,
                    against_team=opp.name,
                    weight=_CASUALTY_WEIGHT[sev],
                ))
    return out


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
    src = "replay event log" if a.has_event_log else "summary totals"
    lines.append(f"  Pivotal plays ({len(a.pivotal)}, from {src}):")
    if not a.pivotal:
        lines.append("    (no scoring or casualties recorded)")
    for i, p in enumerate(a.pivotal, 1):
        lines.append(f"    {i:2d}. [{p.weight:.2f}] {p.headline()}")
    lines.append("")
    return "\n".join(lines)
