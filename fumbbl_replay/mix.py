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
# Voice is the primary; SFX sit underneath as a backdrop. Volumes
# below are tuned to make the commentary clearly the foreground.
SFX_THUD_DELAY_MS = 0
SFX_CROWD_DELAY_MS = 500
# Give the SFX a proper beat to land before the commentary starts.
TTS_PRIMARY_DELAY_MS = 1800
# Gap between play-by-play and colour-commentator reaction. Longer
# pause so each phrase gets room to breathe.
TTS_BANTER_GAP_MS = 750
# SFX volumes pulled WAY down so they don't drown the voice.
SFX_THUD_VOLUME = 0.35
SFX_CROWD_VOLUME = 0.22
TTS_VOLUME = 1.0
# Tail pad after the last input ends so the crowd doesn't clip
# mid-cheer in some encoders.
TAIL_PAD_MS = 600


def _audio_duration_ms(path: Path) -> int:
    """Return clip duration in milliseconds via ffprobe; 0 on failure."""
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, check=True,
        )
        seconds = float(proc.stdout.strip())
        return int(seconds * 1000)
    except Exception:
        return 0


def mix_play_audio(
    tts_path: Path | None,
    sfx_paths: Iterable[Path],
    out_path: Path,
    *,
    tts_banter_path: Path | None = None,
    impact_offset_ms: int = 0,
    target_duration_ms: int | None = None,
) -> Path | None:
    """Mix one play's audio into a single mp3.

    `impact_offset_ms` shifts all on-field SFX and the voice lines so
    they fire at the moment the visual climax lands in the play GIF
    (e.g. when the injury dice settle), not at t=0. The crowd bed
    starts 500 ms after impact; the play-by-play voice 1.3 s after;
    the banter follows the play-by-play. The pre-impact stretch is
    silence so the audience sees the movement first.

    `target_duration_ms` pads the mixed output with trailing silence
    so audio length >= the gif length — that lets the compose step
    play the gif to completion without having to loop it.

    Returns the output Path, or None if ffmpeg is missing or all
    inputs are empty.
    """
    if not shutil.which("ffmpeg"):
        log.warning("ffmpeg not found on PATH; cannot mix audio")
        return None
    inputs: list[tuple[Path, float, int]] = []   # (path, volume, delay_ms)
    sfx_list = [p for p in sfx_paths if p and p.exists()]
    sfx_thud_delay = impact_offset_ms + SFX_THUD_DELAY_MS
    sfx_crowd_delay = impact_offset_ms + SFX_CROWD_DELAY_MS
    tts_primary_delay = impact_offset_ms + TTS_PRIMARY_DELAY_MS
    if sfx_list:
        inputs.append((sfx_list[0], SFX_THUD_VOLUME, sfx_thud_delay))
    if len(sfx_list) > 1:
        inputs.append((sfx_list[1], SFX_CROWD_VOLUME, sfx_crowd_delay))
    if tts_path and tts_path.exists():
        inputs.append((tts_path, TTS_VOLUME, tts_primary_delay))
    if tts_banter_path and tts_banter_path.exists():
        primary_dur = _audio_duration_ms(tts_path) if tts_path else 0
        banter_delay = tts_primary_delay + primary_dur + TTS_BANTER_GAP_MS
        inputs.append((tts_banter_path, TTS_VOLUME, banter_delay))
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
    # Tail pad: at minimum a small breath; bump to `target_duration_ms`
    # if the caller wants the audio to match the gif length so the
    # video can play the gif to completion without looping.
    if target_duration_ms and target_duration_ms > 0:
        filt_parts.append(
            f"[mix]apad=whole_dur={target_duration_ms / 1000.0}[out]"
        )
        final_label = "[out]"
    elif TAIL_PAD_MS:
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
    *,
    banter_by_play: dict[int, Path] | None = None,
    impact_offsets_ms: dict[int, int] | None = None,
    target_durations_ms: dict[int, int] | None = None,
) -> dict[int, Path]:
    """Mix the entire match's per-play audio. Returns {play_index -> mp3 Path}.

    `impact_offsets_ms` and `target_durations_ms` (both keyed by play
    index) come from the gif renderer when video sync is wired up.
    Missing entries fall back to t=0 / no pad.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    out: dict[int, Path] = {}
    banter_by_play = banter_by_play or {}
    impact_offsets_ms = impact_offsets_ms or {}
    target_durations_ms = target_durations_ms or {}
    all_indices = sorted(set(tts_by_play) | set(sfx_by_play))
    for idx in all_indices:
        kind = kinds_by_play.get(idx, "play")
        path = output_dir / f"{idx:02d}_{kind}.mp3"
        result = mix_play_audio(
            tts_path=tts_by_play.get(idx),
            sfx_paths=sfx_by_play.get(idx, []),
            out_path=path,
            tts_banter_path=banter_by_play.get(idx),
            impact_offset_ms=impact_offsets_ms.get(idx, 0),
            target_duration_ms=target_durations_ms.get(idx),
        )
        if result:
            out[idx] = result
    return out
