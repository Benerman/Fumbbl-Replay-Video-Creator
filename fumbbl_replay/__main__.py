"""CLI entry point.

Usage:
    python -m fumbbl_replay <replay-ref> [--json]

`<replay-ref>` is one of:
    * a FUMBBL replay URL, e.g. https://fumbbl.com/ffblive.jnlp?replay=1901135
    * a local .jnlp file path
    * a bare game id, e.g. 1901135

Prints a ranked list of pivotal plays. With `--json` emits a structured
report to stdout for downstream tools.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import sys

from . import analyzer, fumbbl_api, jnlp_loader


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fumbbl-replay")
    parser.add_argument(
        "ref",
        help="FUMBBL replay URL, .jnlp file path, or bare game id",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON instead of text")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    ref = jnlp_loader.resolve(args.ref)
    logging.getLogger("fumbbl_replay").info(
        "resolved gameId=%d via %s", ref.game_id, ref.source
    )

    summary = fumbbl_api.fetch_match_summary(ref.game_id)
    analysis = analyzer.analyze(summary)

    if args.json:
        out = dataclasses.asdict(analysis)
        print(json.dumps(out, indent=2))
    else:
        print(analyzer.format_report(analysis))

    return 0


if __name__ == "__main__":
    sys.exit(main())
