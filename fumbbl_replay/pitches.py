"""Fetch and cache the FUMBBL pitch backgrounds.

FFB's default pitch set is published as a ZIP at
`https://fumbbl.com/FUMBBL/Images/Pitches/Default.zip` containing
five 782x452 PNGs plus a `pitch.ini` mapping. Each PNG is one
weather-themed pitch background (nice grass / sun-bleached / heat /
rain / blizzard). We download the ZIP once on first use, unpack the
PNGs to the on-disk cache, and serve them as PIL images keyed by
the BB weather display name carried in `replay.game.fieldModel.weather`.
"""

from __future__ import annotations

import io
import logging
import os
import zipfile
from pathlib import Path

import requests
from PIL import Image

log = logging.getLogger(__name__)

_DEFAULT_ZIP_URL = "https://fumbbl.com/FUMBBL/Images/Pitches/Default.zip"

# Weather display name (as it appears in the replay JSON) -> ZIP filename.
# Mirrors Weather.java's (displayName, shortName) pairs and FFB's pitch.ini.
_WEATHER_TO_FILE = {
    "Nice Weather":    "nice.png",
    "Very Sunny":      "sunny.png",
    "Sweltering Heat": "heat.png",
    "Pouring Rain":    "rain.png",
    "Blizzard":        "blizzard.png",
}
# Fallback for empty / unknown weather: the calm-day backdrop.
_DEFAULT_WEATHER = "Nice Weather"

_CACHE_DIR = Path(os.environ.get(
    "FUMBBL_REPLAY_CACHE",
    str(Path.home() / ".cache" / "fumbbl-replay-video-creator")
)) / "pitches"

_pitch_cache: dict[str, Image.Image] = {}


_SHORT_TO_FILE = {
    "nice": "nice.png", "sunny": "sunny.png", "heat": "heat.png",
    "rain": "rain.png", "blizzard": "blizzard.png",
}


def fetch_pitch_by_short_name(short: str) -> Image.Image | None:
    """Force a specific pitch (nice / sunny / heat / rain / blizzard)."""
    filename = _SHORT_TO_FILE.get(short)
    if not filename:
        return None
    if filename in _pitch_cache:
        return _pitch_cache[filename]
    cache_path = _CACHE_DIR / filename
    if not cache_path.exists():
        try:
            _download_and_unzip()
        except Exception as e:
            log.warning("could not fetch FUMBBL pitches zip: %s", e)
            return None
    if not cache_path.exists():
        return None
    im = Image.open(cache_path).convert("RGBA")
    _pitch_cache[filename] = im
    return im


def fetch_pitch(weather: str | None) -> Image.Image | None:
    """Return the pitch background PNG for the given weather, or None
    if we can't resolve / fetch it."""
    if not weather:
        weather = _DEFAULT_WEATHER
    filename = _WEATHER_TO_FILE.get(weather) or _WEATHER_TO_FILE[_DEFAULT_WEATHER]
    if filename in _pitch_cache:
        return _pitch_cache[filename]
    cache_path = _CACHE_DIR / filename
    if not cache_path.exists():
        try:
            _download_and_unzip()
        except Exception as e:
            log.warning("could not fetch FUMBBL pitches zip: %s", e)
            return None
    if not cache_path.exists():
        return None
    im = Image.open(cache_path).convert("RGBA")
    _pitch_cache[filename] = im
    return im


def _download_and_unzip() -> None:
    log.info("GET %s", _DEFAULT_ZIP_URL)
    r = requests.get(_DEFAULT_ZIP_URL, timeout=60)
    r.raise_for_status()
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        for name in z.namelist():
            if name.endswith(".png"):
                cache_path = _CACHE_DIR / Path(name).name
                cache_path.write_bytes(z.read(name))


def weather_from_replay(replay: dict) -> str | None:
    """Pull the weather string out of the replay's fieldModel snapshot.
    Also scans the gameLog for `gameSetWeather` deltas in case the
    snapshot was reset, returning the most recent value found."""
    weather = ((replay.get("game") or {}).get("fieldModel") or {}).get("weather")
    for c in replay.get("gameLog", {}).get("commandArray", []) or []:
        for m in c.get("modelChangeList", {}).get("modelChangeArray", []) or []:
            if m.get("modelChangeId") == "fieldModelSetWeather" and m.get("modelChangeValue"):
                weather = m.get("modelChangeValue")
    return weather
