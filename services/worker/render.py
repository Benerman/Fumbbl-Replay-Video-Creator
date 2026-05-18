"""Thin wrapper around `fumbbl_replay.__main__.main(argv)`.

Captures stdout/stderr so the worker can surface failures back to
the user without leaking the entire log into Discord. Produces TWO
final MP4s (16:9 regular and 9:16 Shorts-safe) plus the analyser
output dict that feeds YouTube title/description/tags.

Why two renders: the existing renderer composes pitch + captions
differently for vertical vs horizontal orientations — the layout
isn't just a different crop. So we invoke it twice. The expensive
per-match assets (replay JSON, commentary text, TTS audio, sprites,
SFX, pitch backgrounds) cache through fumbbl_replay's own disk
cache, so the second render is faster than the first.
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
from services.worker import aspect

log = logging.getLogger(__name__)


@dataclass
class RenderResult:
    regular_mp4: Path        # 1920x1080
    short_mp4: Path          # 1080x1920 with Shorts safe-zone padding
    analysis: dict[str, Any]
    work_dir: Path


def render_match(match_ref: str, work_dir: Path) -> RenderResult:
    """Run the full render pipeline twice (h + v); post-process both.

    The work_dir is fully owned by this job: caller is responsible
    for cleaning it up on success. Layout:
      <work_dir>/
        raw_horizontal.mp4    (1576x1252 from fumbbl_replay)
        raw_vertical.mp4      (960x1804  from fumbbl_replay)
        highlight_16x9.mp4    (1920x1080 — uploaded as regular)
        highlight_short.mp4   (1080x1920 — uploaded as Short)
        horizontal/           (per-orientation intermediates)
        vertical/
        mixed/  audio/        (orientation-independent intermediates)
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    horizontal_raw = work_dir / "raw_horizontal.mp4"
    vertical_raw = work_dir / "raw_vertical.mp4"

    _render_orientation(match_ref, "horizontal", horizontal_raw)
    _render_orientation(match_ref, "vertical", vertical_raw)

    regular_mp4 = work_dir / "highlight_16x9.mp4"
    short_mp4 = work_dir / "highlight_short.mp4"

    if not aspect.to_16x9(horizontal_raw, regular_mp4):
        raise RuntimeError(
            f"failed to post-process {horizontal_raw.name} to 1920x1080"
        )
    if not aspect.to_shorts_9x16(vertical_raw, short_mp4):
        raise RuntimeError(
            f"failed to post-process {vertical_raw.name} to 1080x1920 Shorts"
        )

    analysis = _capture_analysis(match_ref, work_dir)

    return RenderResult(
        regular_mp4=regular_mp4,
        short_mp4=short_mp4,
        analysis=analysis,
        work_dir=work_dir,
    )


def _render_orientation(match_ref: str, orientation: str, mp4_path: Path) -> None:
    """One pass of the underlying renderer. Raises on failure."""
    argv = [
        match_ref,
        "--video", str(mp4_path),
        "--orientation", orientation,
    ]
    out_buf = io.StringIO()
    err_buf = io.StringIO()
    log.info("render orientation=%s argv=%s", orientation, argv)
    try:
        with redirect_stdout(out_buf), redirect_stderr(err_buf):
            rc = replay_main.main(argv)
    except SystemExit as e:
        rc = int(e.code) if isinstance(e.code, int) else 1

    # fumbbl_replay.main() doesn't always reach `return 0` — it falls
    # through on success and Python returns None. Treat None as success
    # and use the MP4 existing on disk as the real source of truth.
    rendered_ok = (rc in (None, 0)) and mp4_path.exists()
    if not rendered_ok:
        err_tail = err_buf.getvalue().strip().splitlines()[-50:]
        raise RuntimeError(
            f"fumbbl_replay {orientation} render failed (rc={rc}, "
            f"mp4_exists={mp4_path.exists()}).\n"
            + "\n".join(err_tail)
        )


def _capture_analysis(match_ref: str, work_dir: Path) -> dict[str, Any]:
    """Second pass: analyser JSON for YouTube metadata.

    Cheap because the underlying replay JSON is now in fumbbl_replay's
    cache from the render passes above.
    """
    argv_json = [match_ref, "--json", "--no-replay"]
    json_buf = io.StringIO()
    try:
        with redirect_stdout(json_buf), redirect_stderr(io.StringIO()):
            replay_main.main(argv_json)
    except SystemExit:
        pass
    try:
        analysis = json.loads(json_buf.getvalue())
        (work_dir / "analysis.json").write_text(json.dumps(analysis, indent=2))
        return analysis
    except (ValueError, OSError):
        log.warning("could not capture analysis JSON for %s", match_ref)
        return {}
