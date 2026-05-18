"""Post-process renderer output into exact YouTube canvas sizes.

The renderer emits:
  - vertical:   960x1804  (~9:17, close to 9:16)
  - horizontal: 1576x1252 (~5:4, not 16:9)

YouTube wants:
  - regular: 1920x1080 (16:9)
  - Short:   1080x1920 (9:16) with safe-zone padding so the Shorts
             UI overlays (title at top, description + CTAs at bottom)
             don't obscure pitch / dice / captions.

Both transforms keep aspect ratio by letterboxing into the target
canvas. Cropping is intentionally avoided: viewers need to see all
dice and text, so black bars are the lesser evil.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)


# Shorts safe zone on a 1080x1920 canvas. Measured against YouTube's
# mobile Shorts UI as of late 2024:
#   top reserved:    ~280px (status chrome + title + more-videos chip)
#   bottom reserved: ~380px (description + profile + like/share CTAs)
# Effective inner content box: 1080 wide x 1260 tall, centered.
SHORTS_SAFE_TOP_PX = 280
SHORTS_SAFE_BOTTOM_PX = 380
SHORTS_SAFE_W = 1080
SHORTS_SAFE_H = 1920 - SHORTS_SAFE_TOP_PX - SHORTS_SAFE_BOTTOM_PX  # 1260

REGULAR_W = 1920
REGULAR_H = 1080


def to_16x9(src: Path, dst: Path) -> bool:
    """Scale + letterbox source to exact 1920x1080.

    Source aspect is preserved; black bars fill the remainder.
    Horizontal renderer output (1576x1252) gets ~280px side bars.
    """
    vf = (
        # Fit into 1920x1080 box, height- or width-bound depending on
        # whether the source is wider or narrower than 16:9.
        f"scale=w='if(gt(a,{REGULAR_W}/{REGULAR_H}),{REGULAR_W},-2)':"
        f"h='if(gt(a,{REGULAR_W}/{REGULAR_H}),-2,{REGULAR_H})':flags=lanczos,"
        f"pad={REGULAR_W}:{REGULAR_H}:(ow-iw)/2:(oh-ih)/2:color=black,"
        f"setsar=1"
    )
    return _run_filter(src, dst, vf)


def to_shorts_9x16(src: Path, dst: Path) -> bool:
    """Scale source into the Shorts safe inner box, pad to 1080x1920.

    Content sits at y={SHORTS_SAFE_TOP_PX}, never touching the top
    (title bar) or bottom (description) UI overlays. Side bars
    are slim so the right-rail CTAs only graze the edge.
    """
    inner_w = SHORTS_SAFE_W
    inner_h = SHORTS_SAFE_H
    vf = (
        f"scale=w='if(gt(a,{inner_w}/{inner_h}),{inner_w},-2)':"
        f"h='if(gt(a,{inner_w}/{inner_h}),-2,{inner_h})':flags=lanczos,"
        # Pad to 1080x1920 with the inner box anchored at safe_top, centered horizontally.
        f"pad=1080:1920:(1080-iw)/2:{SHORTS_SAFE_TOP_PX}+({inner_h}-ih)/2:color=black,"
        f"setsar=1"
    )
    return _run_filter(src, dst, vf)


def _run_filter(src: Path, dst: Path, vf: str) -> bool:
    """ffmpeg with the given video filter; re-encode audio for safety.

    Audio is re-encoded (rather than copied) because the source uses
    AAC LC at 192k which we want to preserve verbatim, but some
    container/timestamp combos confuse stream-copy with the resulting
    pad'd video stream's PTS rebase.
    """
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(src),
        "-vf", vf,
        "-c:v", "libx264", "-preset", "slow", "-crf", "18",
        "-pix_fmt", "yuv420p",
        "-profile:v", "main", "-level:v", "4.0", "-bf", "0",
        "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
        "-movflags", "+faststart",
        str(dst),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        log.info("post-processed %s -> %s", src.name, dst.name)
        return True
    except subprocess.CalledProcessError as e:
        log.error("ffmpeg post-process failed (%s): %s",
                  src.name, (e.stderr or "").strip()[-500:])
        return False
