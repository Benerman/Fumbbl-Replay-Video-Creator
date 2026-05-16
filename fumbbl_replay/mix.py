"""Per-play audio mixdown: TTS narration over FFB game SFX.

For each pivotal play we pair the synthesised commentary line with
the FFB on-field sound (td.ogg, injury.ogg, ...) and a spectator-bed
reaction (specCheer / specStomp / specBoo / ...). ffmpeg's filtergraph
layers them:

  - SFX 1 (on-field thud) starts at t=0
  - SFX 2 (crowd bed) starts at t=~700ms so it doesn't drown the thud
  - TTS narration starts at t=~400ms, scaled to centre under the
    final length

Output is a single `.mp3` per play, named the same way as the TTS
clips so downstream tooling (the eventual ffmpeg compose step) can
look them up by `{play_index:02d}_{kind}.mp3`.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path
from typing import Iterable

log = logging.getLogger(__name__)

# Mix offsets / volumes (ms / linear gain).
SFX_THUD_DELAY_MS = 0
SFX_CROWD_DELAY_MS = 700
TTS_DELAY_MS = 400
SFX_THUD_VOLUME = 0.85
SFX_CROWD_VOLUME = 0.55
TTS_VOLUME = 1.0
# After the last input ends, pad the mix with a touch of silence so
# the crowd doesn't clip mid-cheer in some encoders.
TAIL_PAD_MS = 250


def mix_play_audio(
    tts_path: Path | None,
    sfx_paths: Iterable[Path],
    out_path: Path,
) -> Path | None:
    """Mix one play's TTS narration over its SFX into a single mp3.

    Returns the output Path, or None if ffmpeg is missing or all
    inputs are empty. If only one source is supplied we still run
    ffmpeg so the output is always a normalised mp3.
    """
    if not shutil.which("ffmpeg"):
        log.warning("ffmpeg not found on PATH; cannot mix audio")
        return None
    inputs: list[tuple[Path, float, int]] = []   # (path, volume, delay_ms)
    sfx_list = [p for p in sfx_paths if p and p.exists()]
    if sfx_list:
        inputs.append((sfx_list[0], SFX_THUD_VOLUME, SFX_THUD_DELAY_MS))
    if len(sfx_list) > 1:
        inputs.append((sfx_list[1], SFX_CROWD_VOLUME, SFX_CROWD_DELAY_MS))
    if tts_path and tts_path.exists():
        inputs.append((tts_path, TTS_VOLUME, TTS_DELAY_MS))
    if not inputs:
        log.warning("no inputs to mix for %s", out_path.name)
        return None

    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    for path, _, _ in inputs:
        cmd += ["-i", str(path)]

    # Build filter graph: each input gets adelay + volume; amix combines.
    filt_parts: list[str] = []
    for idx, (_, vol, delay) in enumerate(inputs):
        # adelay needs per-channel values; "all=1" applies to all channels.
        filt_parts.append(f"[{idx}:a]adelay={delay}:all=1,volume={vol}[a{idx}]")
    mix_inputs = "".join(f"[a{idx}]" for idx in range(len(inputs)))
    filt_parts.append(
        f"{mix_inputs}amix=inputs={len(inputs)}:duration=longest:normalize=0[mix]"
    )
    if TAIL_PAD_MS:
        filt_parts.append(f"[mix]apad=pad_dur={TAIL_PAD_MS / 1000.0}[out]")
        final_label = "[out]"
    else:
        final_label = "[mix]"
    cmd += ["-filter_complex", ";".join(filt_parts), "-map", final_label]
    cmd += ["-c:a", "libmp3lame", "-b:a", "192k", str(out_path)]

    out_path.parent.mkdir(parents=True, exist_ok=True)
    log.debug("ffmpeg cmd: %s", " ".join(cmd))
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        log.warning("ffmpeg mix failed for %s: %s", out_path.name, e.stderr or e)
        return None
    return out_path


def mix_match_audio(
    tts_by_play: dict[int, Path],
    sfx_by_play: dict[int, list[Path]],
    kinds_by_play: dict[int, str],
    output_dir: Path,
) -> dict[int, Path]:
    """Mix the entire match's per-play audio. Returns {play_index -> mp3 Path}."""
    output_dir.mkdir(parents=True, exist_ok=True)
    out: dict[int, Path] = {}
    # Iterate the union of play indices that have either TTS or SFX
    # since we still want a clip when only one source exists.
    all_indices = sorted(set(tts_by_play) | set(sfx_by_play))
    for idx in all_indices:
        kind = kinds_by_play.get(idx, "play")
        path = output_dir / f"{idx:02d}_{kind}.mp3"
        result = mix_play_audio(
            tts_path=tts_by_play.get(idx),
            sfx_paths=sfx_by_play.get(idx, []),
            out_path=path,
        )
        if result:
            out[idx] = result
    return out
