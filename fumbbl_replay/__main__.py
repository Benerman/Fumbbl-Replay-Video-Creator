"""CLI entry point.

Usage:
    # Fetch a replay via the FFB websocket protocol, save the raw event stream.
    python -m fumbbl_replay fetch <jnlp-source> [--dump out/replay.ndjson]

    # Quick analyzer against the FUMBBL match API (no websocket).
    # Falls back to summary-level pivotal plays.
    python -m fumbbl_replay summary <match-id>

`<jnlp-source>` is either:
    * a FUMBBL replay URL, e.g. https://fumbbl.com/ffblive.jnlp?replay=1901135
    * a local path to a .jnlp file you've saved

Network reachability: the `fetch` subcommand needs to reach the FFB
live server on port 22223 (default). That port is firewalled from
many cloud sandboxes; run from a machine that can reach it.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import sys
from pathlib import Path

from . import analyzer, ffb_client, fumbbl_api, jnlp_loader


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fumbbl-replay")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_fetch = sub.add_parser("fetch", help="Fetch full replay via FFB websocket")
    p_fetch.add_argument("source", help="JNLP URL or local .jnlp path")
    p_fetch.add_argument(
        "--dump", type=Path, default=None,
        help="Write the raw replay stream as NDJSON to this file",
    )
    p_fetch.add_argument(
        "--timeout", type=float, default=30.0,
        help="Idle read timeout in seconds (default: 30)",
    )
    p_fetch.add_argument("--verbose", "-v", action="store_true")

    p_summary = sub.add_parser("summary", help="Quick analysis from match summary API")
    p_summary.add_argument("match_id", type=int)
    p_summary.add_argument("--json", action="store_true")
    p_summary.add_argument("--no-rosters", action="store_true")
    p_summary.add_argument("--verbose", "-v", action="store_true")

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if getattr(args, "verbose", False) else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.cmd == "fetch":
        return _cmd_fetch(args)
    if args.cmd == "summary":
        return _cmd_summary(args)
    parser.error(f"unknown command {args.cmd!r}")
    return 2


def _cmd_fetch(args) -> int:
    info = jnlp_loader.load(args.source)
    log = logging.getLogger("fumbbl_replay")
    log.info("parsed JNLP: %s", info.as_dict())

    def progress(d: ffb_client.ReplayDump) -> None:
        if d.total_commands:
            log.info("  ... %d/%d commands", d.received_commands, d.total_commands)
        else:
            log.info("  ... %d commands so far", d.received_commands)

    dump = ffb_client.fetch_replay(info, timeout_s=args.timeout, on_progress=progress)
    inner = dump.replay_commands()
    log.info(
        "received %d server messages, %d replay commands inside (lastCommand=%s)",
        len(dump.server_messages), len(inner), dump.last_seen,
    )

    if args.dump:
        ffb_client.save_dump(dump, args.dump)

    # Tally the most common netCommandIds so the user can see the shape.
    counts: dict[str, int] = {}
    for cmd in inner:
        cid = cmd.get("netCommandId", "<missing>")
        counts[cid] = counts.get(cid, 0) + 1
    log.info("inner command-id histogram (top 10):")
    for cid, n in sorted(counts.items(), key=lambda x: x[1], reverse=True)[:10]:
        log.info("  %6d  %s", n, cid)

    return 0


def _cmd_summary(args) -> int:
    summary = fumbbl_api.fetch_match_summary(args.match_id)
    team_home = team_away = None
    if not args.no_rosters:
        team_home = fumbbl_api.fetch_team(int(summary["team1"]["id"]))
        team_away = fumbbl_api.fetch_team(int(summary["team2"]["id"]))
    analysis = analyzer.analyze(summary, team_home, team_away)
    if args.json:
        print(json.dumps(dataclasses.asdict(analysis), indent=2))
    else:
        print(analyzer.format_report(analysis))
    return 0


if __name__ == "__main__":
    sys.exit(main())
