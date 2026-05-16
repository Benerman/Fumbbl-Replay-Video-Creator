"""Generate one whimsical commentary line per pivotal play.

Pluggable backends:

  * `ollama` (default)  - POST to a local Ollama server at
                          http://localhost:11434 with the configured model.
                          No API key needed; runs fully offline.
  * `openai`            - POST to any OpenAI-compatible /v1/chat/completions
                          endpoint (LM Studio, vLLM, llama-cpp-python's
                          OpenAI server, etc.). Configure with
                          OPENAI_BASE_URL + OPENAI_API_KEY env vars or pass
                          base_url/api_key on the call.
  * `claude`            - the Anthropic API path the previous implementation
                          used; needs ANTHROPIC_API_KEY.

All backends share the same system prompt and ask the model to return
JSON of shape `{"lines": [{"play_index": int, "line": str}, ...]}`.
We parse it locally - no SDK-level structured-output support is required
because most local models speak JSON well enough when asked.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

import requests

from .analyzer import MatchAnalysis

log = logging.getLogger(__name__)

DEFAULT_BACKEND = "template"           # no AI, no install, deterministic
DEFAULT_OLLAMA_MODEL = "llama3.2:3b"
DEFAULT_OLLAMA_URL = "http://localhost:11434"
DEFAULT_OPENAI_MODEL = "gpt-4o-mini"
DEFAULT_CLAUDE_MODEL = "claude-opus-4-7"
MAX_TOKENS = 4000
REQUEST_TIMEOUT = 180

SYSTEM = """You are a Blood Bowl highlight-reel commentator. Voice:
slightly theatrical, dry humour, vivid - think 1990s sports anchor
crossed with a pub football pundit who has seen too many fouls.

For every pivotal play passed in, write ONE commentary line. Rules:

* Single sentence, 8-25 words. No second sentence. No two openers in
  the set may be the same.
* Name the actor (scorer / victim / inflicter) and the action. Lean
  into Blood Bowl's absurdity: blood, mud, baying fans, casualties as
  dark comedy. TDs are triumphant; kills are gleeful or grim; tying
  / game-winning scores are dramatic.
* NEVER explain rules. NEVER hedge ("perhaps", "it seems"). NEVER
  literally name turn or half numbers - translate them ("late in the
  half", "as the clock ran down", "barely two turns in").
* Use the `tags` field to colour the line: a `game_winning` TD should
  feel decisive; a `comeback` TD should feel like a swing; a
  `crowd_push` casualty should feel chaotic; a `foul` casualty should
  feel underhanded.

Return ONLY valid JSON of the form:
{"lines":[{"play_index":<int>,"line":"<text>"},...]}
Cover every play in the input; the play_index must match the input.
No prose around the JSON, no markdown fences, just the object."""


def generate_commentary(
    analysis: MatchAnalysis,
    *,
    backend: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
) -> dict[int, str]:
    """Generate one commentary line per pivotal play.

    Returns a {1-based play_index -> commentary line} dict. Plays the
    model omits from its response simply won't appear in the dict; the
    caller falls back to the plain headline for those.

    Backend / model resolution order:
      1. explicit `backend` / `model` arguments
      2. FUMBBL_COMMENTARY_BACKEND / _MODEL env vars
      3. defaults: backend=ollama, model=llama3.2:3b
    """
    if not analysis.pivotal:
        return {}

    backend = backend or os.environ.get("FUMBBL_COMMENTARY_BACKEND", DEFAULT_BACKEND)
    model = model or os.environ.get("FUMBBL_COMMENTARY_MODEL")

    log.info("requesting commentary for %d pivotal plays via %s", len(analysis.pivotal), backend)
    if backend == "template":
        # Fully local, no install, no network. Deterministic templates
        # filled from the structured pivotal-play data.
        from .commentary_templates import render_template_lines
        return render_template_lines(analysis)

    user_prompt = _build_user_prompt(analysis)
    if backend == "ollama":
        text = _call_ollama(user_prompt, model or DEFAULT_OLLAMA_MODEL,
                             base_url or os.environ.get("OLLAMA_BASE_URL", DEFAULT_OLLAMA_URL))
    elif backend == "openai":
        text = _call_openai(user_prompt, model or DEFAULT_OPENAI_MODEL,
                              base_url or os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
                              api_key or os.environ.get("OPENAI_API_KEY", ""))
    elif backend == "claude":
        text = _call_claude(user_prompt, model or DEFAULT_CLAUDE_MODEL)
    else:
        raise ValueError(f"unknown commentary backend {backend!r}; choose template|ollama|openai|claude")

    parsed = _parse_lines(text)
    return {item["play_index"]: item["line"] for item in parsed if "play_index" in item and "line" in item}


def _call_ollama(user_prompt: str, model: str, base_url: str) -> str:
    """POST to Ollama's /api/chat. format='json' coerces the model to JSON."""
    url = f"{base_url.rstrip('/')}/api/chat"
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": user_prompt},
        ],
        "stream": False,
        "format": "json",          # Ollama-specific: forces JSON-mode output
        "options": {
            "num_predict": MAX_TOKENS,
            "temperature": 0.7,
        },
    }
    log.info("POST %s (model=%s)", url, model)
    r = requests.post(url, json=body, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return r.json()["message"]["content"]


def _call_openai(user_prompt: str, model: str, base_url: str, api_key: str) -> str:
    """POST to any /v1/chat/completions endpoint (OpenAI proper, LM Studio,
    vLLM, llama-cpp-python's openai server, etc.)."""
    url = f"{base_url.rstrip('/')}/chat/completions"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": MAX_TOKENS,
        "temperature": 0.7,
        "response_format": {"type": "json_object"},
    }
    log.info("POST %s (model=%s)", url, model)
    r = requests.post(url, json=body, headers=headers, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def _call_claude(user_prompt: str, model: str) -> str:
    """Original Anthropic path. Returns the model's raw JSON text."""
    import anthropic
    client = anthropic.Anthropic()
    log.info("POST anthropic /v1/messages (model=%s)", model)
    response = client.messages.create(
        model=model,
        max_tokens=MAX_TOKENS,
        system=[{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user_prompt + "\n\nReturn ONLY the JSON object."}],
    )
    return next(block.text for block in response.content if block.type == "text")


def _parse_lines(text: str) -> list[dict[str, Any]]:
    """Local models sometimes wrap JSON in prose or markdown fences;
    extract the first {...} object and parse it."""
    if not text:
        return []
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    else:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            text = text[start:end + 1]
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        log.warning("could not parse commentary JSON: %s; head=%r", e, text[:200])
        return []
    lines = data.get("lines") or []
    return [l for l in lines if isinstance(l, dict)]


def _build_user_prompt(analysis: MatchAnalysis) -> str:
    plays = []
    for i, p in enumerate(analysis.pivotal, 1):
        plays.append({
            "i": i,
            "kind": p.kind,
            "detail": p.detail or None,
            "team": p.team_name,
            "vs": p.against_team,
            "actor": p.player_name,
            "inflicter": p.inflicter_name,
            "inflicter_team": p.inflicter_team,
            "score_after": [p.score_home, p.score_away],
            "half": p.half,
            "turn": p.turn,
            "tags": p.tags,
            "reason": p.reason,
            "injury": p.injury_label,
        })
    return (
        f"Match: {analysis.summary_line()}\n"
        f"Final winner: {analysis.winner or 'draw'}\n\n"
        f"Pivotal plays (write ONE line per play, in order):\n"
        f"{json.dumps(plays, indent=2)}"
    )
