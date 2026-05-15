"""CLI entry point.

Usage:
    python -m fumbbl_replay <match-id> [--json]

`<match-id>` is the FUMBBL match id (the integer that appears in the
URL of `https://fumbbl.com/p/match?id=N`). The script fetches the
match summary, both team rosters, and prints a ranked list of
pivotal plays plus the asset URLs (team logos, player portraits)
we have to draw with.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import sys

from . import analyzer, fumbbl_api


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fumbbl-replay")
    parser.add_argument("match_id", type=int, help="FUMBBL match id (integer)")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of text")
    parser.add_argument(
        "--no-rosters",
        action="store_true",
        help="skip fetching team rosters (faster, but no logos/portraits)",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

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
