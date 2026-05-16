"""Turn commentary lines into per-play audio clips.

Pluggable backends, matching the commentary module:

  * `say` (default, macOS)  - shells out to `/usr/bin/say -v VOICE -o PATH TEXT`.
                              Free, offline, zero install on macOS.
  * `pyttsx3` (cross-platform) - wraps the OS's native TTS (SAPI on Windows,
                              NSSpeechSynthesizer on macOS, espeak on Linux).
                              Optional dependency.
  * `openai`                - any OpenAI-compatible /v1/audio/speech endpoint.
                              Requires OPENAI_API_KEY (or override).

Output: one audio file per pivotal play, named `{idx:02d}_{kind}.{ext}`.
Returns a {play_index -> Path} dict so the caller (ffmpeg compose step)
can align audio with the matching GIF.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_BACKEND = "say"
# Gravelly macOS novelty voice that reads like an orc / goblin coach
# yelling from the sidelines. Override with --tts-voice for a different
# race vibe (Cellos = dramatic boomer, Bahh = snotling, Trinoids =
# skaven warpstone radio, Cellos = vampire, etc.).
DEFAULT_SAY_VOICE = "Bad News"
DEFAULT_PYTTSX3_VOICE: str | None = None  # let pyttsx3 pick
DEFAULT_OPENAI_MODEL = "tts-1"
DEFAULT_OPENAI_VOICE = "alloy"


def generate_audio(
    commentary_lines: dict[int, str],
    output_dir: Path,
    pivotal_kinds: dict[int, str] | None = None,
    *,
    backend: str | None = None,
    voice: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
) -> dict[int, Path]:
    """Render one audio clip per commentary line. Returns {play_index -> Path}.

    `pivotal_kinds` is an optional {play_index -> kind-name} so output
    files include the play kind in the filename (matches the tableaux /
    gifs naming convention).
    """
    if not commentary_lines:
        return {}

    backend = backend or os.environ.get("FUMBBL_TTS_BACKEND", DEFAULT_BACKEND)
    output_dir.mkdir(parents=True, exist_ok=True)
    pivotal_kinds = pivotal_kinds or {}

    if backend == "say":
        renderer = _SayBackend(voice or os.environ.get("FUMBBL_TTS_VOICE", DEFAULT_SAY_VOICE))
    elif backend == "pyttsx3":
        renderer = _Pyttsx3Backend(voice or os.environ.get("FUMBBL_TTS_VOICE", DEFAULT_PYTTSX3_VOICE))
    elif backend == "openai":
        renderer = _OpenAIBackend(
            voice=voice or os.environ.get("FUMBBL_TTS_VOICE", DEFAULT_OPENAI_VOICE),
            base_url=base_url or os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
            api_key=api_key or os.environ.get("OPENAI_API_KEY", ""),
            model=os.environ.get("FUMBBL_TTS_MODEL", DEFAULT_OPENAI_MODEL),
        )
    else:
        raise ValueError(f"unknown TTS backend {backend!r}; choose say|pyttsx3|openai")

    log.info("synthesising %d commentary lines via %s (voice=%s)",
             len(commentary_lines), backend, renderer.voice)
    out: dict[int, Path] = {}
    for idx in sorted(commentary_lines):
        line = commentary_lines[idx]
        kind = pivotal_kinds.get(idx, "play")
        filename = f"{idx:02d}_{kind}.{renderer.extension}"
        path = output_dir / filename
        try:
            renderer.render(line, path)
            out[idx] = path
        except Exception as e:
            log.warning("TTS render failed for play %d (%s): %s", idx, kind, e)
    return out


# ---------------- backends ----------------

class _SayBackend:
    extension = "aiff"

    def __init__(self, voice: str):
        if not shutil.which("say"):
            raise RuntimeError("/usr/bin/say not found; --tts-backend say only works on macOS")
        self.voice = voice

    def render(self, text: str, path: Path) -> None:
        # say -v VOICE -o PATH TEXT. Quote-safe via argv list.
        cmd = ["say", "-v", self.voice, "-o", str(path), text]
        log.debug("running: %s", cmd)
        subprocess.run(cmd, check=True, capture_output=True)


class _Pyttsx3Backend:
    extension = "wav"

    def __init__(self, voice: str | None):
        try:
            import pyttsx3  # type: ignore
        except ImportError as e:
            raise RuntimeError("pyttsx3 not installed; pip install pyttsx3") from e
        self._engine = pyttsx3.init()
        if voice:
            self._engine.setProperty("voice", voice)
        self.voice = voice or "default"

    def render(self, text: str, path: Path) -> None:
        self._engine.save_to_file(text, str(path))
        self._engine.runAndWait()


class _OpenAIBackend:
    extension = "mp3"

    def __init__(self, *, voice: str, base_url: str, api_key: str, model: str):
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is required for the openai TTS backend")
        self.voice = voice
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model

    def render(self, text: str, path: Path) -> None:
        import requests
        url = f"{self.base_url}/audio/speech"
        headers = {"Authorization": f"Bearer {self.api_key}",
                   "Content-Type": "application/json"}
        body = {"model": self.model, "voice": self.voice, "input": text}
        r = requests.post(url, json=body, headers=headers, timeout=120, stream=True)
        r.raise_for_status()
        with path.open("wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)
