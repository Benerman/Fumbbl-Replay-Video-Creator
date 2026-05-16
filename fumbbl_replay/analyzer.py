"""Identify pivotal plays from a FUMBBL match.

Given a match summary, optionally enriched with the two team rosters
and the per-event replay timeline, emit a ranked list of plays that
mattered most.

A pivotal play is one of:

  * a touchdown
  * an interception
  * an injury (BH / SI / RIP)

Base weights:  TD 1.0  RIP 0.8  INT 0.7  SI 0.5  BH 0.2.

When events from the replay log are supplied we apply context
modifiers - a tying TD scored late in the second half outscores a
runaway-game TD; a foul-induced kill outscores a routine block-cas;
a crowd-push casualty rates lower than a thumping block. Tags
explaining each modifier are exposed on the PivotalPlay so downstream
code (commentary, video) can lean on them.

Without events we fall back to summary totals only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .events import Event, PlayerInfo
from .fumbbl_api import image_url


_BASE_CASUALTY_WEIGHT = {"rip": 0.8, "si": 0.5, "bh": 0.2}
_BASE_TD_WEIGHT = 1.0
_BASE_INT_WEIGHT = 0.7

# TD context modifiers (additive, capped at +1.0 total)
_TD_MOD_GAME_WINNING = 0.6
_TD_MOD_TYING = 0.4
_TD_MOD_COMEBACK = 0.3
_TD_MOD_LATE = 0.1
_TD_MOD_CAP = 1.0

# Casualty context modifiers
_CAS_MOD_FOUL = 0.2
_CAS_MOD_CROWD = -0.1


@dataclass
class PivotalPlay:
    kind: str          # "touchdown" | "casualty" | "interception"
    detail: str        # "RIP" / "SI" / "BH" for casualties; "" otherwise
    team_id: int
    team_name: str
    against_team: str
    weight: float
    half: int | None = None
    turn: int | None = None
    command_nr: int | None = None    # gameLog command number, for field reconstruction
    score_home: int | None = None
    score_away: int | None = None
    player_id: str | None = None
    player_name: str | None = None
    inflicter_id: str | None = None
    inflicter_name: str | None = None
    inflicter_team: str | None = None
    injury_label: str | None = None
    reason: str | None = None        # casualty: "blocked" / "fouled" / "crowdPushed"
    tags: list[str] = field(default_factory=list)

    def headline(self) -> str:
        when = self._when_phrase()
        if self.kind == "touchdown":
            verb = self._td_verb()
            scorer = f"{self.player_name} ({self.team_name})" if self.player_name else self.team_name
            return f"{scorer} {verb}{when}"
        if self.kind == "interception":
            actor = f"{self.player_name} ({self.team_name})" if self.player_name else self.team_name
            return f"{actor} intercepted a pass{when}"
        # casualty
        sev = {"rip": "killed", "si": "seriously injured", "bh": "knocked out"}.get(
            self.detail.lower(), self.detail
        )
        if self.reason == "crowdPushed":
            sev = "shoved off the pitch (crowd push)" if self.detail.lower() == "bh" else f"{sev} after being shoved off the pitch"
        elif self.reason == "fouled":
            sev = f"{sev} by a foul"
        by = ""
        if self.inflicter_name and self.reason != "crowdPushed":
            by = f" by {self.inflicter_name}" + (f" ({self.inflicter_team})" if self.inflicter_team else "")
        victim = f"{self.player_name} ({self.team_name})" if self.player_name else f"a {self.team_name} player"
        label = f" - {self.injury_label}" if self.injury_label and self.detail.lower() != "rip" else ""
        return f"{victim} was {sev}{by}{when}{label}"

    def _td_verb(self) -> str:
        if "game_winning" in self.tags:
            return "scored the game-winning touchdown"
        if "tying" in self.tags:
            return "scored a tying touchdown"
        if "comeback" in self.tags:
            return "scored a comeback touchdown"
        return "scored a touchdown"

    def _when_phrase(self) -> str:
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
        return team.player_name(player_id)

    def resolve_inflicter(player_id: str | None) -> tuple[str | None, str | None]:
        if not player_id:
            return None, None
        info = player_lookup.get(str(player_id))
        if info:
            team_name = home.name if info.side == "home" else away.name
            return info.name, team_name
        return None, None

    # The game-winning TD is the eventual winner's earliest TD that pushed
    # them past the loser's *final* score (e.g. in a 1-2 game, the away TD
    # that made it 1-2 is game-winning; the away TD that only tied 1-1 is
    # not - that's a comeback or tying tag).
    final_winner_side: str | None = None
    final_loser_score = 0
    if home.score > away.score:
        final_winner_side, final_loser_score = "home", away.score
    elif away.score > home.score:
        final_winner_side, final_loser_score = "away", home.score
    game_winning_td: Event | None = None
    if final_winner_side:
        for e in events:
            if e.kind != "touchdown" or e.side != final_winner_side:
                continue
            winner_post = e.score_home if final_winner_side == "home" else e.score_away
            if winner_post > final_loser_score:
                game_winning_td = e
                break

    out: list[PivotalPlay] = []
    for e in events:
        team = home if e.side == "home" else away
        opp = away if e.side == "home" else home
        if e.kind == "touchdown":
            tags: list[str] = []
            # Pre-event score: subtract 1 from the scoring side.
            pre_home = e.score_home - (1 if e.side == "home" else 0)
            pre_away = e.score_away - (1 if e.side == "away" else 0)
            scoring_pre = pre_home if e.side == "home" else pre_away
            opp_pre = pre_away if e.side == "home" else pre_home
            if e.score_home == e.score_away:
                tags.append("tying")
            if scoring_pre < opp_pre:
                tags.append("comeback")
            if e is game_winning_td:
                tags.append("game_winning")
            if e.half == 2 and e.turn and e.turn >= 5:
                tags.append("late")
            weight = _BASE_TD_WEIGHT + min(_TD_MOD_CAP, _td_modifier(tags))
            out.append(PivotalPlay(
                kind="touchdown", detail="",
                team_id=team.id, team_name=team.name, against_team=opp.name,
                weight=weight,
                half=e.half or None, turn=e.turn or None,
                command_nr=e.command_nr,
                score_home=e.score_home, score_away=e.score_away,
                player_id=e.player_id,
                player_name=resolve_name(team, e.player_id),
                tags=tags,
            ))
        elif e.kind == "interception":
            out.append(PivotalPlay(
                kind="interception", detail="",
                team_id=team.id, team_name=team.name, against_team=opp.name,
                weight=_BASE_INT_WEIGHT,
                half=e.half or None, turn=e.turn or None,
                command_nr=e.command_nr,
                score_home=e.score_home, score_away=e.score_away,
                player_id=e.player_id,
                player_name=resolve_name(team, e.player_id),
                tags=[],
            ))
        elif e.kind in ("kill", "serious_injury", "badly_hurt"):
            sev = {"kill": "rip", "serious_injury": "si", "badly_hurt": "bh"}[e.kind]
            tags = []
            mod = 0.0
            if e.reason == "fouled":
                tags.append("foul")
                mod += _CAS_MOD_FOUL
            elif e.reason == "crowdPushed":
                tags.append("crowd_push")
                mod += _CAS_MOD_CROWD
            inflicter_name, inflicter_team = resolve_inflicter(e.inflicter_id)
            out.append(PivotalPlay(
                kind="casualty", detail=sev.upper(),
                team_id=team.id, team_name=team.name, against_team=opp.name,
                weight=max(0.0, _BASE_CASUALTY_WEIGHT[sev] + mod),
                half=e.half or None, turn=e.turn or None,
                command_nr=e.command_nr,
                score_home=e.score_home, score_away=e.score_away,
                player_id=e.player_id,
                player_name=resolve_name(team, e.player_id),
                inflicter_id=e.inflicter_id,
                inflicter_name=inflicter_name,
                inflicter_team=inflicter_team,
                injury_label=e.detail,
                reason=e.reason,
                tags=tags,
            ))
    return out


def _td_modifier(tags: list[str]) -> float:
    m = 0.0
    if "game_winning" in tags:
        m += _TD_MOD_GAME_WINNING
    if "tying" in tags:
        m += _TD_MOD_TYING
    if "comeback" in tags:
        m += _TD_MOD_COMEBACK
    if "late" in tags:
        m += _TD_MOD_LATE
    return m


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
                    weight=_BASE_CASUALTY_WEIGHT[sev],
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
        weight=_BASE_TD_WEIGHT,
    )


def format_report(a: MatchAnalysis, *, commentary: dict[int, str] | None = None) -> str:
    commentary = commentary or {}
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
        if i in commentary:
            lines.append(f"        “{commentary[i]}”")
    lines.append("")
    return "\n".join(lines)
