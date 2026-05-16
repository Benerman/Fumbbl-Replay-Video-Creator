"""Generate one whimsical commentary line per pivotal play via Claude.

Sends the entire pivotal-play list in a single batched call, asking
Claude to return one short narration line per play. Structured output
(`messages.parse` with a Pydantic model) keeps parsing reliable. The
system prompt is marked cacheable so repeated runs against multiple
matches in the same session don't re-pay for the voice/style guide.

Requires `anthropic` (added to requirements.txt) and the
ANTHROPIC_API_KEY environment variable.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import anthropic
from pydantic import BaseModel

from .analyzer import MatchAnalysis

log = logging.getLogger(__name__)

MODEL = "claude-opus-4-7"
MAX_TOKENS = 4000

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

Return a JSON object with `lines`, an array of {play_index, line}.
Cover every play in the input; the play_index must match the input."""


class CommentaryLine(BaseModel):
    play_index: int
    line: str


class Commentary(BaseModel):
    lines: list[CommentaryLine]


def generate_commentary(analysis: MatchAnalysis, *, model: str = MODEL) -> dict[int, str]:
    """Generate one commentary line per pivotal play.

    Returns a {1-based play_index -> commentary line} dict. Plays
    omitted from the model's response just won't appear in the dict;
    the caller falls back to the plain headline for those.
    """
    if not analysis.pivotal:
        return {}

    client = anthropic.Anthropic()
    user_prompt = _build_user_prompt(analysis)
    log.info("requesting commentary for %d pivotal plays via %s", len(analysis.pivotal), model)

    response = client.messages.parse(
        model=model,
        max_tokens=MAX_TOKENS,
        system=[
            {"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}},
        ],
        messages=[{"role": "user", "content": user_prompt}],
        output_format=Commentary,
    )
    return {item.play_index: item.line for item in response.parsed_output.lines}


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
