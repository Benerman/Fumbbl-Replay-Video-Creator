"""Turn commentary lines into per-play audio clips.

Pluggable backends, matching the commentary module:

  * `kokoro` (default)      - local neural TTS via the kokoro-onnx package.
                              Near-ElevenLabs quality, runs on CPU, no API
                              key. Auto-downloads the ~310MB ONNX model and
                              ~28MB voices file into the shared cache on
                              first use. Needs Python 3.10+ and
                              `pip install kokoro-onnx soundfile`.
  * `say` (macOS)           - shells out to `/usr/bin/say -v VOICE -o PATH TEXT`.
                              Voices are robotic novelty fare; kept for
                              fallback and "Premium" downloaded voices.
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

DEFAULT_BACKEND = "kokoro"
DEFAULT_KOKORO_VOICE = "am_michael"     # US male, natural sports-anchor read
DEFAULT_SAY_VOICE = "Bad News"          # gravelly novelty fallback
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

    if backend == "kokoro":
        renderer = _KokoroBackend(voice or os.environ.get("FUMBBL_TTS_VOICE", DEFAULT_KOKORO_VOICE))
    elif backend == "say":
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
        raise ValueError(f"unknown TTS backend {backend!r}; choose kokoro|say|pyttsx3|openai")

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

_KOKORO_MODEL_URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx"
_KOKORO_VOICES_URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin"


class _KokoroBackend:
    """Local neural TTS via kokoro-onnx. Model + voice files cached on
    disk under the shared fumbbl-replay cache; first call downloads
    them (~310MB + 28MB)."""
    extension = "wav"
    _instance = None  # lazy singleton; model load is the slow bit

    def __init__(self, voice: str):
        from pathlib import Path as _Path
        try:
            from kokoro_onnx import Kokoro  # type: ignore
            import soundfile  # type: ignore  # noqa: F401
        except ImportError as e:
            raise RuntimeError(
                "kokoro-onnx (and soundfile) required for the kokoro TTS backend. "
                "Install with `pip install kokoro-onnx soundfile` (needs Python 3.10+)."
            ) from e
        cache_dir = _Path(os.environ.get(
            "FUMBBL_REPLAY_CACHE",
            str(_Path.home() / ".cache" / "fumbbl-replay-video-creator")
        )) / "kokoro"
        cache_dir.mkdir(parents=True, exist_ok=True)
        model_path = cache_dir / "kokoro-v1.0.onnx"
        voices_path = cache_dir / "voices-v1.0.bin"
        self._download_if_missing(model_path, _KOKORO_MODEL_URL)
        self._download_if_missing(voices_path, _KOKORO_VOICES_URL)
        if _KokoroBackend._instance is None:
            log.info("loading Kokoro model from %s", cache_dir)
            _KokoroBackend._instance = Kokoro(str(model_path), str(voices_path))
        self._kokoro = _KokoroBackend._instance
        self.voice = voice

    @staticmethod
    def _download_if_missing(dest, url: str) -> None:
        if dest.exists():
            return
        import requests
        log.info("downloading %s (~%s)",
                 url.rsplit('/', 1)[-1],
                 "330MB" if "onnx" in str(dest) else "28MB")
        r = requests.get(url, stream=True, timeout=300)
        r.raise_for_status()
        with dest.open("wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 20):
                f.write(chunk)

    def render(self, text: str, path: Path) -> None:
        import soundfile as sf  # local import; lib already imported above
        samples, sample_rate = self._kokoro.create(text, voice=self.voice, speed=1.0)
        sf.write(str(path), samples, sample_rate)


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
