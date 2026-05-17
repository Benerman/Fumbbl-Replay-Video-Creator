"""Thin wrapper around `fumbbl_replay.__main__.main(argv)`.

Captures stdout/stderr so the worker can surface failures back to
the user without leaking the entire log into Discord. Returns the
final MP4 path and the analyser output dict so the uploader can
build a useful title/description.
"""

from __future__ import annotations

import io
import json
import logging
import sys
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fumbbl_replay import __main__ as replay_main

log = logging.getLogger(__name__)


@dataclass
class RenderResult:
    mp4_path: Path
    analysis: dict[str, Any]
    work_dir: Path


def render_match(match_ref: str, work_dir: Path) -> RenderResult:
    """Run the full render pipeline; return paths + analysis JSON.

    The work_dir is fully owned by this job: caller is responsible
    for cleaning it up on success.
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    mp4_path = work_dir / "highlight.mp4"
    analysis_path = work_dir / "analysis.json"

    # First pass: render the MP4. fumbbl_replay's argparse forces
    # --commentary on when --tts is set, and --tts on when --mix is
    # set, etc.; --video chains the lot. We add --json to capture
    # the analyser output as a side-effect.
    argv = [
        match_ref,
        "--video", str(mp4_path),
        "--orientation", "vertical",
    ]

    out_buf = io.StringIO()
    err_buf = io.StringIO()
    log.info("render argv=%s", argv)
    try:
        with redirect_stdout(out_buf), redirect_stderr(err_buf):
            rc = replay_main.main(argv)
    except SystemExit as e:
        rc = int(e.code) if isinstance(e.code, int) else 1

    if rc != 0 or not mp4_path.exists():
        err_tail = err_buf.getvalue().strip().splitlines()[-50:]
        raise RuntimeError(
            "fumbbl_replay render failed (rc=" + str(rc) + ").\n"
            + "\n".join(err_tail)
        )

    # Second pass: capture analyser JSON cheaply (cached replay reused).
    argv_json = [match_ref, "--json", "--no-replay"]
    json_buf = io.StringIO()
    try:
        with redirect_stdout(json_buf), redirect_stderr(io.StringIO()):
            replay_main.main(argv_json)
    except SystemExit:
        pass
    try:
        analysis = json.loads(json_buf.getvalue())
        analysis_path.write_text(json.dumps(analysis, indent=2))
    except (ValueError, OSError):
        # Non-fatal; uploader falls back to bare title.
        analysis = {}
        log.warning("could not capture analysis JSON for %s", match_ref)

    return RenderResult(mp4_path=mp4_path, analysis=analysis, work_dir=work_dir)
