"""Turn a raw FUMBBL replay into a chronological list of game events.

The replay dict from `/api/replay/get/{id}/gz` carries
`gameLog.commandArray`: a sequence of `serverModelSync` commands, each
with a `modelChangeList.modelChangeArray` of small typed deltas. State
like the current half, per-team turn number, and running score is
sticky - we carry the last seen value forward and stamp it on each
event we emit.

Events emitted: touchdown, kill, serious_injury, badly_hurt,
interception. Each carries the post-event score so downstream code
can tag game-winning / tying / comeback plays without re-walking
the stream.

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
    kind: str            # "touchdown" | "kill" | "serious_injury" | "badly_hurt" | "interception"
    side: str            # "home" | "away" — for TDs/INTs the actor's side; for casualties the victim's side
    command_nr: int
    half: int            # 1 or 2 (0 before first half starts)
    turn: int            # team-turn number when the event resolved
    score_home: int = 0  # home score AFTER this event
    score_away: int = 0  # away score AFTER this event
    player_id: str | None = None       # scorer / victim / interceptor
    inflicter_id: str | None = None    # for casualties: who blocked/fouled the victim
    detail: str | None = None          # injury label e.g. "Dead (RIP)", "Head Injury (-AV)"
    reason: str | None = None          # for casualties: "blocked" / "fouled" / "crowdPushed"


def extract_events(replay: dict[str, Any]) -> list[Event]:
    cmds: Iterable[dict[str, Any]] = replay.get("gameLog", {}).get("commandArray", []) or []
    half = 0
    turn_home = 0
    turn_away = 0
    score_home = 0
    score_away = 0
    events: list[Event] = []

    for c in cmds:
        if c.get("netCommandId") != "serverModelSync":
            continue
        changes = c.get("modelChangeList", {}).get("modelChangeArray", []) or []
        cn = int(c.get("commandNr", 0) or 0)

        # Phase 1: update sticky state (half, turn, score) from this command's deltas.
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
            elif mid == "teamResultSetScore" and isinstance(v, int):
                if m.get("modelChangeKey") == "home":
                    score_home = v
                elif m.get("modelChangeKey") == "away":
                    score_away = v

        # Phase 2: collect per-command companion fields.
        # The casualty victim is the key of `playerResultSetSeriousInjury`
        # (serious injuries only) or, for plain BH, the key of the only
        # `playerResultSetSendToBoxReason`. The scorer is the key of
        # `playerResultSetTouchdowns`. Reason and inflicter co-locate
        # with the casualty in `playerResultSetSendToBox*` fields keyed
        # on the victim.
        scorer: str | None = None
        interceptor: str | None = None
        victim: str | None = None
        injury_label: str | None = None
        send_box: dict[str, dict[str, Any]] = {}  # victim_id -> {reason, by, half, turn}
        for m in changes:
            mid = m.get("modelChangeId")
            key = m.get("modelChangeKey")
            v = m.get("modelChangeValue")
            if mid == "playerResultSetTouchdowns" and key:
                scorer = str(key)
            elif mid == "playerResultSetInterceptions" and key:
                interceptor = str(key)
            elif mid == "playerResultSetSeriousInjury" and key:
                victim = str(key)
                if v is not None:
                    injury_label = str(v)
            elif mid == "playerResultSetSendToBoxReason" and key:
                send_box.setdefault(str(key), {})["reason"] = v
            elif mid == "playerResultSetSendToBoxByPlayerId" and key:
                send_box.setdefault(str(key), {})["by"] = str(v) if v else None

        # Phase 3: emit events keyed on the team-result counter changes.
        for m in changes:
            mid = m.get("modelChangeId")
            side = m.get("modelChangeKey")
            v = m.get("modelChangeValue")
            event_turn = turn_home if side == "home" else turn_away
            if mid == "teamResultSetScore" and side in ("home", "away"):
                events.append(Event(
                    kind="touchdown", side=side, command_nr=cn,
                    half=half, turn=event_turn,
                    score_home=score_home, score_away=score_away,
                    player_id=scorer,
                ))
            elif mid == "teamResultSetRipSuffered" and side in ("home", "away"):
                meta = send_box.get(victim or "", {})
                events.append(Event(
                    kind="kill", side=side, command_nr=cn,
                    half=half, turn=event_turn,
                    score_home=score_home, score_away=score_away,
                    player_id=victim, detail=injury_label,
                    inflicter_id=meta.get("by"),
                    reason=meta.get("reason"),
                ))
            elif mid == "teamResultSetSeriousInjurySuffered" and side in ("home", "away"):
                if injury_label and "RIP" in injury_label.upper():
                    continue  # already emitted as kill
                meta = send_box.get(victim or "", {})
                events.append(Event(
                    kind="serious_injury", side=side, command_nr=cn,
                    half=half, turn=event_turn,
                    score_home=score_home, score_away=score_away,
                    player_id=victim, detail=injury_label,
                    inflicter_id=meta.get("by"),
                    reason=meta.get("reason"),
                ))
            elif mid == "teamResultSetBadlyHurtSuffered" and side in ("home", "away"):
                # BH victim is the key of the SendToBoxReason record where the inflicter is on the OPPOSING side.
                bh_victim = _bh_victim(send_box, victim_excluded=victim)
                meta = send_box.get(bh_victim or "", {}) if bh_victim else {}
                events.append(Event(
                    kind="badly_hurt", side=side, command_nr=cn,
                    half=half, turn=event_turn,
                    score_home=score_home, score_away=score_away,
                    player_id=bh_victim,
                    inflicter_id=meta.get("by"),
                    reason=meta.get("reason"),
                ))
        # Interception: emit when interceptor seen in this command.
        # The thrower's team loses the ball; the interceptor's side gets the event.
        if interceptor:
            int_side = _player_side(replay, interceptor)
            if int_side:
                events.append(Event(
                    kind="interception", side=int_side, command_nr=cn,
                    half=half,
                    turn=turn_home if int_side == "home" else turn_away,
                    score_home=score_home, score_away=score_away,
                    player_id=interceptor,
                ))

    return events


def _bh_victim(send_box: dict[str, dict[str, Any]], *, victim_excluded: str | None) -> str | None:
    """Pick the most likely BH victim from same-command SendToBox records.

    The serious-injury path keys the victim explicitly via
    `playerResultSetSeriousInjury`. Plain BHs don't - we just see one
    or more SendToBox records. If exactly one record's player isn't
    already accounted for as an SI/RIP, that's the BH victim.
    """
    candidates = [pid for pid in send_box if pid != victim_excluded]
    return candidates[0] if len(candidates) == 1 else None


def _player_side(replay: dict[str, Any], player_id: str) -> str | None:
    game = replay.get("game") or {}
    for side in ("home", "away"):
        team = game.get(f"team{side.capitalize()}") or {}
        for p in team.get("playerArray") or []:
            if str(p.get("playerId")) == str(player_id):
                return side
    return None
