"""Reconstruct who is where on the pitch at any point in the replay.

The replay's `gameLog.commandArray` carries the field state as a
running diff: `fieldModelSetPlayerCoordinate` places (or moves) a
player, `fieldModelRemovePlayer` takes them off, `fieldModelSetBallCoordinate`
moves the ball. To know where things were at command N we replay
every diff in `[1..N]` and read out the resulting state.

Coordinates: the FFB pitch is 26 wide x 15 tall (x in [0..25],
y in [0..14]). Negative x means off-pitch (dugout / reserves /
KO box). We keep both kinds in the state map so the renderer can
choose what to do with bench players.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

PITCH_WIDTH = 26
PITCH_HEIGHT = 15


@dataclass
class FieldState:
    command_nr: int
    players: dict[str, tuple[int, int]] = field(default_factory=dict)  # playerId -> (x, y)
    player_states: dict[str, int] = field(default_factory=dict)        # playerId -> state bitmask
    ball: tuple[int, int] | None = None
    ball_in_play: bool = False

    def on_pitch(self) -> dict[str, tuple[int, int]]:
        return {pid: (x, y) for pid, (x, y) in self.players.items() if 0 <= x < PITCH_WIDTH and 0 <= y < PITCH_HEIGHT}

    def off_pitch(self) -> dict[str, tuple[int, int]]:
        return {pid: (x, y) for pid, (x, y) in self.players.items() if not (0 <= x < PITCH_WIDTH and 0 <= y < PITCH_HEIGHT)}

    def dugout_counts(self, player_lookup) -> dict[str, dict[str, int]]:
        """Return {'home': {res, ko, bh, si, rip, ban}, 'away': {...}}.

        Buckets by the player-state low byte:
          5 = KO  ('ko')
          6 = BH  ('bh')
          7 = SI  ('si')
          8 = RIP ('rip')
          9 = RESERVE ('res')
         13 = BANNED  ('ban')
        Anything else (standing / moving / prone / stunned / etc.) is
        counted as on-pitch active and isn't bucketed here. Player ids
        the lookup doesn't know about are skipped.
        """
        cats = {5: "ko", 6: "bh", 7: "si", 8: "rip", 9: "res", 13: "ban"}
        out = {
            "home": {k: 0 for k in ("res", "ko", "bh", "si", "rip", "ban")},
            "away": {k: 0 for k in ("res", "ko", "bh", "si", "rip", "ban")},
        }
        for pid, raw_state in self.player_states.items():
            info = player_lookup.get(pid)
            if info is None:
                continue
            base = raw_state & 0xFF
            cat = cats.get(base)
            if cat:
                out[info.side][cat] += 1
        return out


def reconstruct_at(
    replay: dict[str, Any],
    command_nr: int,
    *,
    stop_at: set[str] | None = None,
) -> FieldState:
    """Return the field state at command_nr.

    Within the target command we apply modelChanges in order; if one
    of the modelChangeIds in `stop_at` appears, we stop BEFORE
    applying it. Use this to capture the field at the moment a
    pivotal event resolved, before the post-event cleanup that often
    sweeps every player back into a dugout (after a TD) within the
    same command.
    """
    cmds: Iterable[dict[str, Any]] = replay.get("gameLog", {}).get("commandArray", []) or []
    state = FieldState(command_nr=command_nr)
    stop_at = stop_at or set()
    for c in cmds:
        cn = int(c.get("commandNr", 0) or 0)
        if cn > command_nr:
            break
        if c.get("netCommandId") != "serverModelSync":
            continue
        is_target = cn == command_nr
        for m in c.get("modelChangeList", {}).get("modelChangeArray", []) or []:
            mid = m.get("modelChangeId")
            if is_target and mid in stop_at:
                return state
            key = m.get("modelChangeKey")
            v = m.get("modelChangeValue")
            if mid == "fieldModelSetPlayerCoordinate" and key and isinstance(v, list) and len(v) == 2:
                state.players[str(key)] = (int(v[0]), int(v[1]))
            elif mid == "fieldModelRemovePlayer" and key:
                # Drop the pitch coordinate so the player vanishes from
                # on-pitch rendering, but KEEP their state bitmask —
                # FFB has just set it to KO/BH/SI/RIP/BAN in the prior
                # cmd, and the dugout-strip count needs to see it.
                # Wiping player_states here is why the strip showed
                # zeros all match.
                state.players.pop(str(key), None)
            elif mid == "fieldModelSetPlayerState" and key and isinstance(v, int):
                state.player_states[str(key)] = v
            elif mid == "fieldModelSetBallCoordinate":
                state.ball = (int(v[0]), int(v[1])) if isinstance(v, list) and len(v) == 2 else None
            elif mid == "fieldModelSetBallInPlay":
                state.ball_in_play = bool(v)
    return state
