"""CLI entry point.

Usage:

    python -m fumbbl_replay <replay-ref> [--json] [--dump-replay PATH] [--no-replay] [--no-rosters]

`<replay-ref>` is any of:
    * a FUMBBL replay URL, e.g. https://fumbbl.com/ffblive.jnlp?replay=1901135
    * a local path to a saved .jnlp file
    * a bare match id, e.g. 1901135

The default pipeline pulls the match summary, fetches the gzipped
replay event log over HTTP, identifies pivotal plays (TDs, kills,
injuries) with their player names and turn numbers, and prints a
text report. `--json` switches to structured JSON for downstream
tooling. `--no-replay` skips the event-log step and reports
summary-level totals only.
"""

from __future__ import annotations

import argparse
import dataclasses
import gzip
import json
import logging
import sys
from pathlib import Path

from . import analyzer, events, fumbbl_api, jnlp_loader


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fumbbl-replay")
    parser.add_argument("replay_ref", help="FUMBBL replay URL, .jnlp path, or numeric match id")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of a text report")
    parser.add_argument("--no-replay", action="store_true",
                        help="Skip the replay event log, use summary totals only")
    parser.add_argument("--no-rosters", action="store_true",
                        help="Skip the per-team roster fetch (omits player names from headlines)")
    parser.add_argument("--dump-replay", type=Path, default=None,
                        help="Save the raw replay JSON (gzipped) to this path")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger("fumbbl_replay")

    match_id = jnlp_loader.resolve(args.replay_ref)
    log.info("resolved replay ref to match id %d", match_id)

    summary = fumbbl_api.fetch_match_summary(match_id)

    team_home = team_away = None
    if not args.no_rosters:
        team_home = fumbbl_api.fetch_team(int(summary["team1"]["id"]))
        team_away = fumbbl_api.fetch_team(int(summary["team2"]["id"]))

    event_list = None
    player_lookup = None
    if not args.no_replay:
        replay_id = fumbbl_api.resolve_replay_id(match_id, summary)
        replay = fumbbl_api.fetch_replay(replay_id)
        event_list = events.extract_events(replay)
        player_lookup = events.roster_from_replay(replay)
        log.info("extracted %d events from replay %d (%d in-game players)",
                 len(event_list), replay_id, len(player_lookup))
        if args.dump_replay:
            args.dump_replay.parent.mkdir(parents=True, exist_ok=True)
            with gzip.open(args.dump_replay, "wt", encoding="utf-8") as f:
                json.dump(replay, f)
            log.info("dumped raw replay to %s", args.dump_replay)

    analysis = analyzer.analyze(
        summary, team_home, team_away,
        events=event_list, player_lookup=player_lookup,
    )

    if args.json:
        print(json.dumps(dataclasses.asdict(analysis), indent=2))
    else:
        print(analyzer.format_report(analysis))
    return 0


if __name__ == "__main__":
    sys.exit(main())
