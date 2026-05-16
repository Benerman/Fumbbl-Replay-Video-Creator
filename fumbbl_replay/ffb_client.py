"""Speak the FFB websocket replay protocol to fetch a full replay.

The FFB Java client (`com.fumbbl.ffb.client.FantasyFootballClientAwt`)
connects to `ws://{host}:{port}/command`, sends a `clientReplay`
message with the gameId, and receives a stream of `serverReplay`
batches. Each batch contains a `commandArray` of up to 100 server
commands (model deltas + report lists). When the server sets
`lastCommand: true`, the replay is fully delivered.

This module is the minimal-viable port of that protocol: connect,
send `clientReplay`, collect every server message, return them as a
list of dicts. Higher layers turn the raw stream into events.

Reachability: FFB live runs on port 22223 which is firewalled from
many cloud sandboxes. Run this from a machine that can reach
`fumbbl.com:22223`.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import websocket  # from websocket-client

from .jnlp_loader import JnlpReplayInfo

log = logging.getLogger(__name__)

# How long to wait for a single server message after the last one.
# When we stop seeing messages and we've seen the lastCommand flag,
# we're done. Without lastCommand, this is the polite timeout.
DEFAULT_READ_TIMEOUT_S = 30.0


@dataclass
class ReplayDump:
    info: JnlpReplayInfo
    server_messages: list[dict[str, Any]] = field(default_factory=list)
    total_commands: int = 0
    received_commands: int = 0
    last_seen: bool = False

    def replay_commands(self) -> list[dict[str, Any]]:
        """Flatten `serverReplay` batches into one list of inner ServerCommands."""
        out: list[dict[str, Any]] = []
        for msg in self.server_messages:
            if msg.get("netCommandId") == "serverReplay":
                for inner in msg.get("commandArray", []) or []:
                    out.append(inner)
        return out


def fetch_replay(
    info: JnlpReplayInfo,
    *,
    timeout_s: float = DEFAULT_READ_TIMEOUT_S,
    on_progress: Callable[[ReplayDump], None] | None = None,
) -> ReplayDump:
    """Connect, request the replay, collect all server messages, return."""
    log.info("connecting to %s (gameId=%d, coach=%s)", info.websocket_url, info.game_id, info.coach)
    ws = websocket.create_connection(
        info.websocket_url,
        timeout=timeout_s,
        header=[f"User-Agent: fumbbl-replay-video-creator/0.1"],
    )
    try:
        request = {
            "netCommandId": "clientReplay",
            "gameId": info.game_id,
            "replayToCommandNr": 0,
            "coach": info.coach,
        }
        ws.send(json.dumps(request))
        log.debug("sent clientReplay: %s", request)

        dump = ReplayDump(info=info)
        idle_deadline = time.monotonic() + timeout_s
        while True:
            try:
                ws.settimeout(min(5.0, timeout_s))
                raw = ws.recv()
            except websocket.WebSocketTimeoutException:
                if dump.last_seen:
                    log.info("replay complete (lastCommand seen, no more messages)")
                    break
                if time.monotonic() > idle_deadline:
                    log.warning("read timeout after %.1fs with no lastCommand", timeout_s)
                    break
                continue

            if raw is None or raw == "":
                break

            msg = _parse(raw)
            if msg is None:
                continue
            dump.server_messages.append(msg)
            idle_deadline = time.monotonic() + timeout_s

            if msg.get("netCommandId") == "serverReplay":
                total = int(msg.get("totalNrOfCommands", 0) or 0)
                if total:
                    dump.total_commands = total
                dump.received_commands += len(msg.get("commandArray", []) or [])
                if msg.get("lastCommand"):
                    dump.last_seen = True
                if on_progress:
                    on_progress(dump)

        return dump
    finally:
        try:
            ws.close()
        except Exception:
            pass


def _parse(raw: str | bytes) -> dict[str, Any] | None:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    raw = raw.strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        log.warning("could not parse server message (%s): %.200s", e, raw)
        return None


def save_dump(dump: ReplayDump, path) -> None:
    """Save the full dump as NDJSON: one server message per line."""
    import io
    from pathlib import Path

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as f:
        for msg in dump.server_messages:
            f.write(json.dumps(msg))
            f.write("\n")
    log.info("wrote %d server messages to %s", len(dump.server_messages), p)
