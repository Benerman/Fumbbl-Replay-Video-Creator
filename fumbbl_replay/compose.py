"""Final stitched video: per-play GIFs + per-play mixed-audio MP3s -> MP4.

For each pivotal play we already have:
  - vertical/gifs/{NN_kind_cmd}.gif    (silent, ~3-7s of looping action)
  - mixed/{NN_kind}.mp3                (SFX + voice A + voice B layered)

`compose_highlight_reel` encodes one MP4 per play (gif looped to fill the
audio duration), then concatenates them in pivotal order into a single
match-highlight MP4.

The intermediate per-play MP4s use a fixed codec/profile/timebase so the
final concat step is a stream copy — fast and avoids quality loss from
re-encoding.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Iterable, Sequence

log = logging.getLogger(__name__)

# Encoding profile for the intermediates. The pad ensures even dimensions
# (libx264 requires width/height divisible by 2) regardless of input size.
# CRF 18 is visually near-lossless; preset 'slow' gives notably smaller
# files than 'medium' for an extra second of encode per play, which is
# fine for our 9-clip match-highlight workloads.
VIDEO_CODEC = "libx264"
VIDEO_PRESET = "slow"
VIDEO_CRF = "18"
VIDEO_PIX_FMT = "yuv420p"
AUDIO_CODEC = "aac"
AUDIO_BITRATE = "256k"
FPS = 30


def _audio_duration_seconds(path: Path) -> float:
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, check=True,
        )
        return float(proc.stdout.strip())
    except Exception:
        return 0.0


def _encode_play_clip(video_source: Path, audio: Path, out: Path,
                       *, frames_dir: Path | None = None) -> bool:
    """Encode one play. Stretches the source's final frame to fill the
    audio length: movement → impact → linger-on-impact-while-voice-trails.

    When `frames_dir` is provided (contains `concat.txt`), the high-res
    PNG sequence is used as the video source — no GIF palette
    quantisation, no downscale, full colour depth. Otherwise falls back
    to the GIF at `video_source`.
    """
    audio_dur = _audio_duration_seconds(audio)
    if audio_dur <= 0:
        log.warning("could not read audio duration for %s; skipping", audio.name)
        return False

    using_frames = frames_dir is not None and (frames_dir / "concat.txt").exists()
    if using_frames:
        concat_file = frames_dir / "concat.txt"
        # Sum durations from concat.txt to get the source video length.
        src_dur = 0.0
        for line in concat_file.read_text().splitlines():
            if line.startswith("duration "):
                try:
                    src_dur += float(line.split()[1])
                except (ValueError, IndexError):
                    pass
        src_input = ["-f", "concat", "-safe", "0", "-i", str(concat_file)]
    else:
        src_dur = _audio_duration_seconds(video_source)
        src_input = ["-i", str(video_source)]

    pad_dur = max(0.0, audio_dur - src_dur)
    # tpad=clone holds the last frame; fps re-times to constant rate; pad
    # ensures even dimensions for libx264.
    vf = (
        f"tpad=stop_mode=clone:stop_duration={pad_dur:.3f},"
        f"fps={FPS},pad=ceil(iw/2)*2:ceil(ih/2)*2"
    )
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        *src_input,
        "-i", str(audio),
        "-map", "0:v:0", "-map", "1:a:0",
        "-t", f"{audio_dur:.3f}",
        "-vf", vf,
        "-c:v", VIDEO_CODEC, "-preset", VIDEO_PRESET, "-crf", VIDEO_CRF,
        "-pix_fmt", VIDEO_PIX_FMT,
        "-c:a", AUDIO_CODEC, "-b:a", AUDIO_BITRATE,
        "-movflags", "+faststart",
        str(out),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        return True
    except subprocess.CalledProcessError as e:
        log.warning("ffmpeg encode failed for %s: %s", video_source.name, e.stderr or e)
        return False


def _concat_clips(clips: Sequence[Path], out_path: Path) -> bool:
    """Concat the per-play MP4s into one. Stream-copy where possible."""
    if not clips:
        return False
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        for c in clips:
            # ffmpeg concat demuxer wants `file '/abs/path'` lines.
            f.write(f"file '{c.resolve()}'\n")
        listing = f.name
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", listing,
        "-c", "copy",
        "-movflags", "+faststart",
        str(out_path),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        return True
    except subprocess.CalledProcessError as e:
        # Codec-mismatch fallback: re-encode the concat.
        log.warning("concat -c copy failed (%s); falling back to re-encode", e.stderr.splitlines()[-1] if e.stderr else e)
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "concat", "-safe", "0", "-i", listing,
            "-c:v", VIDEO_CODEC, "-preset", VIDEO_PRESET, "-crf", VIDEO_CRF,
            "-pix_fmt", VIDEO_PIX_FMT,
            "-c:a", AUDIO_CODEC, "-b:a", AUDIO_BITRATE,
            "-movflags", "+faststart",
            str(out_path),
        ]
        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True)
            return True
        except subprocess.CalledProcessError as e2:
            log.warning("concat re-encode also failed: %s", e2.stderr or e2)
            return False
    finally:
        Path(listing).unlink(missing_ok=True)


def compose_highlight_reel(
    gifs_by_play: dict[int, Path],
    audio_by_play: dict[int, Path],
    kinds_by_play: dict[int, str],
    out_path: Path,
    *,
    work_dir: Path | None = None,
    frames_dirs_by_play: dict[int, Path] | None = None,
) -> Path | None:
    """Stitch per-play sources + mixed MP3s into one MP4 highlight reel.

    Plays are concatenated in ascending play-index order. When
    `frames_dirs_by_play[idx]` is provided, that PNG sequence (with
    concat.txt) is used as the video source — full-resolution,
    full-colour. Otherwise falls back to the matching GIF.

    Returns the output path, or None if ffmpeg is missing or no
    clips could be produced.
    """
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        log.warning("ffmpeg/ffprobe not on PATH; cannot compose video")
        return None
    if work_dir is None:
        # Stem-scoped so a vertical + horizontal pair of runs into the
        # same output folder don't trample each other's intermediates.
        work_dir = out_path.parent / f"_clips_{out_path.stem}"
    work_dir.mkdir(parents=True, exist_ok=True)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    frames_dirs_by_play = frames_dirs_by_play or {}

    clips: list[Path] = []
    for idx in sorted(set(gifs_by_play) & set(audio_by_play)):
        gif = gifs_by_play[idx]
        audio = audio_by_play[idx]
        kind = kinds_by_play.get(idx, "play")
        clip = work_dir / f"{idx:02d}_{kind}.mp4"
        if _encode_play_clip(gif, audio, clip,
                             frames_dir=frames_dirs_by_play.get(idx)):
            clips.append(clip)
    if not clips:
        log.warning("no per-play clips produced; nothing to concat")
        return None

    if _concat_clips(clips, out_path):
        log.info("composed %d play(s) into %s", len(clips), out_path)
        return out_path
    return None
