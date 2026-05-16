"""Turn a raw FUMBBL replay into a chronological list of game events.

The replay dict from `/api/replay/get/{id}/gz` carries
`gameLog.commandArray`: a sequence of `serverModelSync` commands, each
with a `modelChangeList.modelChangeArray` of small typed deltas. State
like the current half and per-team turn number is sticky - we carry
the last seen value forward and stamp it on each event we emit.

Today we extract the events we score on (TDs and casualties). The
parser is structured so adding richer events (interceptions, kickoff
results, expulsions) is a matter of recognising more `modelChangeId`s.

The replay carries its OWN in-game roster (under `game.teamHome` /
`game.teamAway`) with the playerIds the event stream uses. Those are
different from the persistent playerIds the `/api/team` endpoint
returns, so we expose a helper that pulls names from the replay
itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


@dataclass
class PlayerInfo:
    player_id: str
    name: str
    number: int | None
    side: str           # "home" | "away"
    position_id: str | None
    portrait_url: str | None


def roster_from_replay(replay: dict[str, Any]) -> dict[str, PlayerInfo]:
    """Build a {playerId -> PlayerInfo} map from the replay's in-game rosters."""
    out: dict[str, PlayerInfo] = {}
    game = replay.get("game") or {}
    for side in ("home", "away"):
        team = game.get(f"team{side.capitalize()}") or {}
        for p in team.get("playerArray") or []:
            pid = str(p.get("playerId") or "")
            if not pid:
                continue
            out[pid] = PlayerInfo(
                player_id=pid,
                name=str(p.get("playerName") or "").strip() or pid,
                number=p.get("playerNr"),
                side=side,
                position_id=str(p.get("positionId") or "") or None,
                portrait_url=p.get("urlPortrait") or None,
            )
    return out


@dataclass
class Event:
    kind: str            # "touchdown" | "kill" | "serious_injury" | "badly_hurt"
    side: str            # "home" | "away" — team the event happened TO (scoring side / victim side)
    command_nr: int
    half: int            # 1 or 2 (0 before first half starts)
    turn: int            # team-turn number when the event resolved
    player_id: str | None = None
    detail: str | None = None  # e.g. "Dead (RIP)", "Head Injury (-AV)"


def extract_events(replay: dict[str, Any]) -> list[Event]:
    cmds: Iterable[dict[str, Any]] = replay.get("gameLog", {}).get("commandArray", []) or []
    half = 0
    turn_home = 0
    turn_away = 0
    events: list[Event] = []

    for c in cmds:
        if c.get("netCommandId") != "serverModelSync":
            continue
        changes = c.get("modelChangeList", {}).get("modelChangeArray", []) or []
        cn = int(c.get("commandNr", 0) or 0)

        # Phase 1: update sticky state from this command's deltas.
        for m in changes:
            mid = m.get("modelChangeId")
            v = m.get("modelChangeValue")
            if mid == "gameSetHalf" and isinstance(v, int):
                half = v
            elif mid == "turnDataSetTurnNr":
                if m.get("modelChangeKey") == "home" and isinstance(v, int):
                    turn_home = v
                elif m.get("modelChangeKey") == "away" and isinstance(v, int):
                    turn_away = v

        # Phase 2: collect the per-command companion fields. The scorer's
        # player id sits in `playerResultSetTouchdowns`; the casualty
        # victim sits in `playerResultSetSeriousInjury` with the injury
        # string as its value.
        scorer: str | None = None
        victim: str | None = None
        injury_label: str | None = None
        for m in changes:
            mid = m.get("modelChangeId")
            if mid == "playerResultSetTouchdowns":
                scorer = str(m.get("modelChangeKey")) if m.get("modelChangeKey") else scorer
            elif mid == "playerResultSetSeriousInjury":
                victim = str(m.get("modelChangeKey")) if m.get("modelChangeKey") else victim
                injury_label = str(m.get("modelChangeValue")) if m.get("modelChangeValue") else injury_label

        # Phase 3: emit events keyed on the team-result counter changes.
        for m in changes:
            mid = m.get("modelChangeId")
            side = m.get("modelChangeKey")
            if side not in ("home", "away"):
                continue
            if mid == "teamResultSetScore":
                events.append(Event(
                    kind="touchdown", side=side, command_nr=cn,
                    half=half,
                    turn=turn_home if side == "home" else turn_away,
                    player_id=scorer,
                ))
            elif mid == "teamResultSetRipSuffered":
                events.append(Event(
                    kind="kill", side=side, command_nr=cn,
                    half=half,
                    turn=turn_home if side == "home" else turn_away,
                    player_id=victim,
                    detail=injury_label,
                ))
            elif mid == "teamResultSetSeriousInjurySuffered":
                # Skip if the injury_label is a RIP — that's already counted as a kill.
                if injury_label and "RIP" in injury_label.upper():
                    continue
                events.append(Event(
                    kind="serious_injury", side=side, command_nr=cn,
                    half=half,
                    turn=turn_home if side == "home" else turn_away,
                    player_id=victim,
                    detail=injury_label,
                ))
            elif mid == "teamResultSetBadlyHurtSuffered":
                events.append(Event(
                    kind="badly_hurt", side=side, command_nr=cn,
                    half=half,
                    turn=turn_home if side == "home" else turn_away,
                ))

    return events
