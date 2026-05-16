"""Generate docs/overview.html: a self-contained project showcase.

Pulls one example match from FUMBBL, runs the analyzer, renders a
handful of tableaux and one animated play, then bakes the lot into a
single HTML file with images inlined as base64 data URIs.

Run from the repo root:

    python -m scripts.build_overview

Output: docs/overview.html (committed alongside this script).
"""

from __future__ import annotations

import base64
import dataclasses
import html
import logging
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fumbbl_replay import analyzer, animate, events, field_state, fumbbl_api, tableau  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("build_overview")


# Example match: a 4-0 Elven Union vs Chaos Chosen game with a clean
# game-winning TD plus a kill - good variety for the showcase.
EXAMPLE_MATCH_ID = 4700842


@dataclass
class Embed:
    title: str
    caption: str
    media_type: str  # "image/png" | "image/gif"
    data_b64: str


def main() -> int:
    summary = fumbbl_api.fetch_match_summary(EXAMPLE_MATCH_ID)
    replay_id = fumbbl_api.resolve_replay_id(EXAMPLE_MATCH_ID, summary)
    replay = fumbbl_api.fetch_replay(replay_id)
    event_list = events.extract_events(replay)
    player_lookup = events.roster_from_replay(replay)
    team_home = fumbbl_api.fetch_team(int(summary["team1"]["id"]))
    team_away = fumbbl_api.fetch_team(int(summary["team2"]["id"]))
    analysis = analyzer.analyze(summary, team_home, team_away,
                                events=event_list, player_lookup=player_lookup)

    embeds = _generate_embeds(replay, analysis, player_lookup)
    repo_tree = _build_tree(ROOT)
    text_report = analyzer.format_report(analysis)

    out_path = ROOT / "docs" / "overview.html"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(_html(analysis, embeds, repo_tree, text_report), encoding="utf-8")
    log.info("wrote %s (%.1f KB)", out_path, out_path.stat().st_size / 1024)
    return 0


def _generate_embeds(replay, analysis, player_lookup) -> list[Embed]:
    """Render fresh tableaux + one GIF into a temp dir, base64 them."""
    embeds: list[Embed] = []
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        # Pick a representative spread: best TD, best casualty, plus one
        # mid-pack each, in the order they appear in the ranked list.
        pivotal = analysis.pivotal
        td_plays = [p for p in pivotal if p.kind == "touchdown"][:2]
        cas_plays = [p for p in pivotal if p.kind == "casualty"][:2]
        chosen = td_plays + cas_plays

        for i, p in enumerate(chosen, 1):
            if p.command_nr is None:
                continue
            if p.kind == "touchdown":
                state = field_state.reconstruct_at(replay, p.command_nr,
                                                    stop_at={"teamResultSetScore"})
            elif p.kind == "casualty":
                state = field_state.reconstruct_at(replay, p.command_nr - 1)
            else:
                state = field_state.reconstruct_at(replay, p.command_nr)
            png = tdp / f"tab{i}.png"
            tableau.render_tableau(p, state, player_lookup, png)
            embeds.append(Embed(
                title=p.headline(),
                caption=f"Pivotal-play tableau (weight {p.weight:.2f}). "
                        f"Yellow ring marks the involved players; ball is the small white circle.",
                media_type="image/png",
                data_b64=base64.b64encode(png.read_bytes()).decode("ascii"),
            ))

        # One GIF: the highest-weight TD's drive.
        if td_plays:
            top_td = td_plays[0]
            gif = tdp / "drive.gif"
            animate.render_play_gif(replay, top_td, player_lookup, gif)
            embeds.append(Embed(
                title=f"Animated drive: {top_td.headline()}",
                caption="Each frame is the field state after a command that moved a player or the ball, "
                        "leading up to the moment of the touchdown.",
                media_type="image/gif",
                data_b64=base64.b64encode(gif.read_bytes()).decode("ascii"),
            ))
    return embeds


def _build_tree(root: Path) -> str:
    """Plain-text tree of the package + scripts + docs dirs."""
    important = ["fumbbl_replay", "scripts", "docs", "README.md", "requirements.txt", "LICENSE"]
    lines: list[str] = []
    for name in important:
        p = root / name
        if not p.exists():
            continue
        if p.is_file():
            lines.append(f"{name}")
        else:
            lines.append(f"{name}/")
            for child in sorted(p.iterdir()):
                if child.name.startswith(".") or child.name == "__pycache__":
                    continue
                lines.append(f"  {child.name}")
    return "\n".join(lines)


def _html(analysis, embeds: list[Embed], repo_tree: str, text_report: str) -> str:
    embed_blocks = "\n".join(_embed_block(e) for e in embeds)

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>FUMBBL Replay Video Creator - overview</title>
<style>
  :root {{
    --bg: #1a1f1a;
    --panel: #232a23;
    --ink: #e8ecdf;
    --dim: #9da595;
    --accent: #f0c14b;
    --home: #3c6ec8;
    --away: #c84638;
    --line: #3a4538;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    background: var(--bg);
    color: var(--ink);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
    line-height: 1.55;
  }}
  main {{
    max-width: 920px;
    margin: 0 auto;
    padding: 56px 32px 96px;
  }}
  h1 {{
    font-size: 36px;
    margin: 0 0 8px;
    letter-spacing: -0.02em;
  }}
  h2 {{
    font-size: 22px;
    margin: 56px 0 12px;
    border-bottom: 1px solid var(--line);
    padding-bottom: 8px;
  }}
  h3 {{
    font-size: 17px;
    margin: 28px 0 8px;
  }}
  p {{ margin: 8px 0 16px; }}
  .lede {{ color: var(--dim); font-size: 18px; margin-bottom: 24px; }}
  code {{
    background: var(--panel);
    padding: 2px 6px;
    border-radius: 3px;
    font-size: 13px;
    color: var(--accent);
  }}
  pre {{
    background: var(--panel);
    padding: 16px 20px;
    border-radius: 6px;
    overflow-x: auto;
    font-size: 13px;
    line-height: 1.5;
    border: 1px solid var(--line);
  }}
  pre code {{ background: none; padding: 0; color: var(--ink); }}
  .pipeline {{
    display: grid;
    gap: 12px;
    margin: 16px 0 24px;
  }}
  .step {{
    background: var(--panel);
    padding: 14px 18px;
    border-radius: 6px;
    border: 1px solid var(--line);
  }}
  .step .num {{
    display: inline-block;
    width: 26px; height: 26px;
    background: var(--accent);
    color: #1a1f1a;
    border-radius: 50%;
    text-align: center;
    line-height: 26px;
    font-weight: 700;
    margin-right: 10px;
  }}
  .step h3 {{ display: inline; font-size: 16px; margin: 0; }}
  .step p {{ margin: 8px 0 0; color: var(--dim); font-size: 14px; }}
  .embed {{
    margin: 24px 0;
    background: var(--panel);
    border-radius: 8px;
    border: 1px solid var(--line);
    overflow: hidden;
  }}
  .embed img {{
    display: block;
    width: 100%;
    height: auto;
    background: #1a1f1a;
  }}
  .embed-title {{
    padding: 12px 18px 4px;
    font-weight: 600;
    color: var(--ink);
  }}
  .embed-cap {{
    padding: 0 18px 14px;
    color: var(--dim);
    font-size: 13px;
  }}
  .legend {{
    display: flex; gap: 18px; flex-wrap: wrap;
    margin: 16px 0;
    font-size: 14px;
  }}
  .legend span {{
    display: inline-flex; align-items: center; gap: 6px;
  }}
  .legend .dot {{
    display: inline-block; width: 14px; height: 14px; border-radius: 50%;
  }}
  .footer {{
    margin-top: 64px;
    padding-top: 24px;
    border-top: 1px solid var(--line);
    color: var(--dim);
    font-size: 13px;
  }}
  a {{ color: var(--accent); }}
  table {{
    width: 100%;
    border-collapse: collapse;
    margin: 12px 0 24px;
    font-size: 14px;
  }}
  th, td {{
    text-align: left;
    padding: 8px 12px;
    border-bottom: 1px solid var(--line);
  }}
  th {{ color: var(--dim); font-weight: 500; }}
  .status-done {{ color: #7ec97e; }}
  .status-todo {{ color: var(--dim); }}
</style>
</head>
<body>
<main>
  <h1>FUMBBL Replay Video Creator</h1>
  <p class="lede">A toolchain for turning a Blood Bowl replay on FUMBBL into a short whimsical highlight reel - this page shows where the project is today.</p>

  <h2>What it does</h2>
  <p>Give it a FUMBBL match id (or a URL or a JNLP file). It pulls down the match summary and the full per-turn event log, finds the plays that mattered most, and prints a ranked report. With <code>--tableaux DIR</code> it also renders a PNG per pivotal play; with <code>--gifs DIR</code> it renders an animated GIF of the run-up.</p>

  <h3>Sample text report (for match {analysis.match_id})</h3>
  <pre><code>{html.escape(text_report)}</code></pre>

  <h2>How the pipeline works</h2>
  <div class="pipeline">
    <div class="step"><span class="num">1</span><h3>Resolve replay reference</h3>
      <p><code>jnlp_loader.resolve()</code> turns a URL, .jnlp file, or bare numeric id into a match id.</p></div>
    <div class="step"><span class="num">2</span><h3>Fetch summary + replay</h3>
      <p><code>/api/match/get/{{id}}</code> gives the scoreboard; <code>/api/replay/get/{{replayId}}/gz</code> streams the full gzipped game log over plain HTTP - the same per-turn deltas the official Java client gets over its websocket.</p></div>
    <div class="step"><span class="num">3</span><h3>Walk the gameLog</h3>
      <p><code>events.extract_events()</code> walks <code>gameLog.commandArray</code>, holds onto sticky state (half, per-team turn, running score), and emits one typed Event per scoring or casualty counter increment - each stamped with the post-event score, the player ids, and the inflicter for casualties.</p></div>
    <div class="step"><span class="num">4</span><h3>Score pivotal plays</h3>
      <p><code>analyzer.analyze()</code> applies context modifiers - a TD that pushes the eventual winner past the loser's final score is "game-winning", a TD that levels the score is "tying", a kill from a foul is rated higher than a routine block. Headlines name the player and turn.</p></div>
    <div class="step"><span class="num">5</span><h3>Reconstruct the field</h3>
      <p>For visuals we rebuild the on-pitch state at any moment: <code>field_state.reconstruct_at(replay, cmd_nr)</code> replays every <code>fieldModelSetPlayerCoordinate</code> / <code>fieldModelRemovePlayer</code> / <code>fieldModelSetBallCoordinate</code> delta up to that command. A <code>stop_at</code> hook halts mid-command - needed because the post-TD cleanup that sweeps every player to the dugout fires in the same command as the score itself.</p></div>
    <div class="step"><span class="num">6</span><h3>Render</h3>
      <p><code>tableau.render_tableau()</code> draws a single still PNG for one play; <code>animate.render_play_gif()</code> stitches frames from the run-up into an animated GIF. Both highlight the involved players (scorer, victim, inflicter) with a yellow ring.</p></div>
  </div>

  <h2>The image format</h2>
  <p>The pitch is the FFB 26x15 grid. Endzones are the leftmost and rightmost columns; the line of scrimmage is the bright line dead-centre. Tokens use team colour and the player's jersey number; the ball is a small white circle wherever it was last placed.</p>
  <div class="legend">
    <span><span class="dot" style="background: var(--home)"></span>Home team</span>
    <span><span class="dot" style="background: var(--away)"></span>Away team</span>
    <span><span class="dot" style="background: var(--accent); width: 18px; height: 18px; border: 3px solid var(--accent); background: transparent;"></span>Involved player (scorer / victim / inflicter)</span>
    <span><span class="dot" style="background: #fff; width: 8px; height: 8px;"></span>Ball</span>
  </div>

  <h2>Sample tableaux</h2>
  {embed_blocks}

  <h2>Repo structure</h2>
  <pre><code>{html.escape(repo_tree)}</code></pre>

  <h2>Try it</h2>
  <pre><code>pip install -r requirements.txt

# Text report for any match id
python -m fumbbl_replay {analysis.match_id}

# JSON for downstream tooling
python -m fumbbl_replay {analysis.match_id} --json

# Render PNG tableaux
python -m fumbbl_replay {analysis.match_id} --tableaux out/tableaux

# Render animated GIF per pivotal play
python -m fumbbl_replay {analysis.match_id} --gifs out/gifs</code></pre>

  <h2>Roadmap</h2>
  <table>
    <tr><th>Stage</th><th>Status</th></tr>
    <tr><td>Resolve replay reference (URL / file / id) -> match id</td><td class="status-done">done</td></tr>
    <tr><td>Fetch the gzipped replay over HTTP</td><td class="status-done">done</td></tr>
    <tr><td>Parse server commands into a typed event timeline</td><td class="status-done">done</td></tr>
    <tr><td>Pivotal plays with player names + half + turn</td><td class="status-done">done</td></tr>
    <tr><td>Context-aware scoring (game-winning, tying, comeback, foul)</td><td class="status-done">done</td></tr>
    <tr><td>Casualty inflicter + reason (blocked / fouled / crowd-pushed)</td><td class="status-done">done</td></tr>
    <tr><td>Pixel-art tableau spike (pitch + tokens at saved coords)</td><td class="status-done">done</td></tr>
    <tr><td>Animated GIFs of pivotal plays</td><td class="status-done">done</td></tr>
    <tr><td>Pixel-art tableau visual identity (sprites, art direction)</td><td class="status-todo">todo</td></tr>
    <tr><td>LLM commentary script + TTS narration</td><td class="status-todo">todo</td></tr>
    <tr><td>ffmpeg compose final mp4</td><td class="status-todo">todo</td></tr>
  </table>

  <p class="footer">Generated from <code>scripts/build_overview.py</code> against match #{analysis.match_id}.</p>
</main>
</body>
</html>
"""


def _embed_block(e: Embed) -> str:
    return f"""  <div class="embed">
    <div class="embed-title">{html.escape(e.title)}</div>
    <img src="data:{e.media_type};base64,{e.data_b64}" alt="{html.escape(e.title)}">
    <div class="embed-cap">{html.escape(e.caption)}</div>
  </div>"""


if __name__ == "__main__":
    sys.exit(main())
