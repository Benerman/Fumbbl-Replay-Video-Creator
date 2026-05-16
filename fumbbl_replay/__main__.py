"""CLI entry point.

Usage:

    python -m fumbbl_replay <replay-ref> [--json] [--dump-replay PATH] [--no-replay] [--no-rosters]

`<replay-ref>` is any of:
    * a FUMBBL replay URL, e.g. https://fumbbl.com/ffblive.jnlp?replay=1901135
    * a local path to a saved .jnlp file
    * a bare match id, e.g. 1901135

The default pipeline pulls the match summary, fetches the gzipped
replay event log over HTTP, identifies pivotal plays (TDs, kills,
injuries) with their player names and turn numbers, and prints a
text report. `--json` switches to structured JSON for downstream
tooling. `--no-replay` skips the event-log step and reports
summary-level totals only.
"""

from __future__ import annotations

import argparse
import dataclasses
import gzip
import json
import logging
import sys
from pathlib import Path

from . import analyzer, events, field_state, fumbbl_api, jnlp_loader, sprites


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fumbbl-replay")
    parser.add_argument("replay_ref", help="FUMBBL replay URL, .jnlp path, or numeric match id")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of a text report")
    parser.add_argument("--no-replay", action="store_true",
                        help="Skip the replay event log, use summary totals only")
    parser.add_argument("--no-rosters", action="store_true",
                        help="Skip the per-team roster fetch (omits player names from headlines)")
    parser.add_argument("--dump-replay", type=Path, default=None,
                        help="Save the raw replay JSON (gzipped) to this path")
    parser.add_argument("--tableaux", type=Path, default=None,
                        help="Render a PNG tableau per pivotal play into this directory")
    parser.add_argument("--gifs", type=Path, default=None,
                        help="Render an animated GIF per pivotal play into this directory")
    parser.add_argument("--no-sprites", action="store_true",
                        help="Skip the FUMBBL position sprite fetch; render plain coloured tokens")
    parser.add_argument("--orientation", choices=("vertical", "horizontal"), default="vertical",
                        help="Pitch orientation in tableaux/GIFs (default: vertical)")
    parser.add_argument("--commentary", action="store_true",
                        help="Generate one whimsical commentary line per pivotal play")
    parser.add_argument("--commentary-backend", choices=("template", "ollama", "openai", "claude"),
                        default=None,
                        help="Commentary backend (default: template - local templates, no LLM, no install; env: FUMBBL_COMMENTARY_BACKEND)")
    parser.add_argument("--commentary-model", default=None,
                        help="Model name for the chosen backend (default per backend; env: FUMBBL_COMMENTARY_MODEL)")
    parser.add_argument("--commentary-base-url", default=None,
                        help="Override base URL for ollama/openai backends (e.g. http://localhost:11434)")
    parser.add_argument("--tts", type=Path, default=None,
                        help="Generate per-play TTS audio into this directory (forces --commentary)")
    parser.add_argument("--tts-backend", choices=("say", "pyttsx3", "openai"), default=None,
                        help="TTS backend (default: say on macOS; env: FUMBBL_TTS_BACKEND)")
    parser.add_argument("--tts-voice", default=None,
                        help="Voice name for the chosen TTS backend (default per backend; env: FUMBBL_TTS_VOICE)")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger("fumbbl_replay")

    ref = jnlp_loader.resolve(args.replay_ref)
    log.info("resolved %s -> match_id=%s replay_id=%s",
             args.replay_ref, ref.match_id, ref.replay_id)

    # Resolution paths:
    #   match_id only  -> fetch summary, derive replay_id from it
    #   replay_id only -> fetch replay, synthesize summary from it
    #   both           -> use both directly
    summary: dict | None = None
    replay = None
    replay_id = ref.replay_id

    if ref.match_id is not None:
        summary = fumbbl_api.fetch_match_summary(ref.match_id)
        if replay_id is None:
            replay_id = fumbbl_api.resolve_replay_id(ref.match_id, summary)

    if not args.no_replay and replay_id is not None:
        replay = fumbbl_api.fetch_replay(replay_id)

    if summary is None:
        if replay is None:
            raise SystemExit("can't proceed: only a replay id was provided AND --no-replay was set")
        summary = fumbbl_api.synthesize_summary_from_replay(replay)
        log.info("synthesized summary from replay (no match id available)")

    team_home = team_away = None
    if not args.no_rosters and summary["team1"]["id"] and summary["team2"]["id"]:
        team_home = fumbbl_api.fetch_team(int(summary["team1"]["id"]))
        team_away = fumbbl_api.fetch_team(int(summary["team2"]["id"]))

    event_list = None
    player_lookup = None
    if replay is not None:
        event_list = events.extract_events(replay)
        player_lookup = events.roster_from_replay(replay)
        log.info("extracted %d events from replay %d (%d in-game players)",
                 len(event_list), replay_id, len(player_lookup))
        if args.dump_replay:
            args.dump_replay.parent.mkdir(parents=True, exist_ok=True)
            with gzip.open(args.dump_replay, "wt", encoding="utf-8") as f:
                json.dump(replay, f)
            log.info("dumped raw replay to %s", args.dump_replay)

    analysis = analyzer.analyze(
        summary, team_home, team_away,
        events=event_list, player_lookup=player_lookup,
    )

    commentary_lines: dict[int, str] = {}
    if args.commentary or args.tts:
        from . import commentary
        try:
            commentary_lines = commentary.generate_commentary(
                analysis,
                backend=args.commentary_backend,
                model=args.commentary_model,
                base_url=args.commentary_base_url,
            )
            log.info("got commentary for %d/%d plays", len(commentary_lines), len(analysis.pivotal))
        except Exception as e:
            log.warning("commentary generation failed: %s", e)

    if args.tts:
        from . import tts
        kinds = {i: p.kind for i, p in enumerate(analysis.pivotal, 1)}
        try:
            audio_paths = tts.generate_audio(
                commentary_lines, args.tts,
                pivotal_kinds=kinds,
                backend=args.tts_backend,
                voice=args.tts_voice,
            )
            log.info("rendered %d audio clips to %s", len(audio_paths), args.tts)
        except Exception as e:
            log.warning("TTS generation failed: %s", e)

    if args.json:
        out = dataclasses.asdict(analysis)
        if commentary_lines:
            out["commentary"] = {str(k): v for k, v in commentary_lines.items()}
        print(json.dumps(out, indent=2))
    else:
        print(analyzer.format_report(analysis, commentary=commentary_lines))

    if (args.tableaux or args.gifs) and replay is None:
        log.warning("--tableaux/--gifs require the replay event log; skipping (--no-replay was set)")
        return 0

    player_sprites: dict = {}
    if (args.tableaux or args.gifs) and not args.no_sprites and player_lookup:
        idx = sprites.position_icon_index_from_replay(replay)
        player_sprites = sprites.build_player_sprites(player_lookup, idx)
        log.info("loaded sprites for %d/%d players", len(player_sprites), len(player_lookup))

    # Team labels + logos for the endzones / pitch watermark.
    home_name = analysis.home.name
    away_name = analysis.away.name
    home_logo_img = None
    away_logo_img = None
    pitch_bg = None
    if args.tableaux or args.gifs:
        home_logo_id = _logo_id_from_team(team_home) or _logo_id_from_replay_team(replay, "home")
        away_logo_id = _logo_id_from_team(team_away) or _logo_id_from_replay_team(replay, "away")
        home_logo_img = sprites.fetch_team_logo(home_logo_id)
        away_logo_img = sprites.fetch_team_logo(away_logo_id)
        # Weather-themed pitch background from FFB's Default.zip.
        from . import pitches
        weather = pitches.weather_from_replay(replay)
        pitch_bg = pitches.fetch_pitch(weather)
        if pitch_bg is not None:
            log.info("loaded pitch background for weather %r", weather)

    if args.tableaux or args.gifs:
        from . import dice as dice_mod

    if args.tableaux:
        from . import tableau  # local import: pillow only loaded when needed
        n = 0
        # TDs: stop before the post-score cleanup that sweeps players
        # to the dugout (cleanup happens after the score event in the
        # same command).
        # Casualties: the victim is removed from the pitch BEFORE
        # the casualty trigger fires within the same command, so
        # snapshot the previous command instead.
        for i, p in enumerate(analysis.pivotal, 1):
            if p.command_nr is None:
                continue
            if p.kind == "touchdown":
                state = field_state.reconstruct_at(
                    replay, p.command_nr, stop_at={"teamResultSetScore"},
                )
            elif p.kind == "casualty":
                state = field_state.reconstruct_at(replay, p.command_nr - 1)
            else:
                state = field_state.reconstruct_at(replay, p.command_nr)
            out = args.tableaux / f"{i:02d}_{p.kind}_{p.command_nr}.png"
            # Casualties: the originating block fired in an earlier cmd, so
            # look back. Blunders (double/triple skull, clutch fail) have the
            # roll in the event cmd itself. TDs and interceptions: scan a
            # short window for the dodges/GFIs that set up the play.
            lookback = {"casualty": 8, "touchdown": 8, "interception": 4,
                         "self_kill": 4, "clutch_fail": 0,
                         "double_skull": 0, "triple_skull": 0}.get(p.kind, 0)
            dice_for_play = dice_mod.extract_for_command(replay, p.command_nr, lookback=lookback)
            tableau.render_tableau(
                p, state, player_lookup or {}, out,
                sprites=player_sprites,
                orientation=args.orientation,
                home_name=home_name, away_name=away_name,
                home_logo=home_logo_img, away_logo=away_logo_img,
                dice=dice_for_play,
                pitch_background=pitch_bg,
            )
            n += 1
        log.info("rendered %d tableaux to %s", n, args.tableaux)

    if args.gifs:
        from . import animate
        n = 0
        for i, p in enumerate(analysis.pivotal, 1):
            if p.command_nr is None:
                continue
            out = args.gifs / f"{i:02d}_{p.kind}_{p.command_nr}.gif"
            animate.render_play_gif(
                replay, p, player_lookup or {}, out,
                sprites=player_sprites,
                orientation=args.orientation,
                home_name=home_name, away_name=away_name,
                home_logo=home_logo_img, away_logo=away_logo_img,
                pitch_background=pitch_bg,
            )
            n += 1
        log.info("rendered %d gifs to %s", n, args.gifs)


def _logo_id_from_team(team: dict | None) -> int | None:
    if not team:
        return None
    bio = team.get("bio") or {}
    return bio.get("image")


def _logo_id_from_replay_team(replay: dict, side: str) -> int | None:
    """Fallback: pull the logo image id out of `logoUrl` in the replay's roster."""
    team = (replay.get("game") or {}).get(f"team{side.capitalize()}") or {}
    url = team.get("logoUrl")
    if not url:
        return None
    # logoUrl is typically "i/12345"; the trailing integer is the image id.
    import re
    m = re.search(r"(\d+)", url)
    return int(m.group(1)) if m else None

    return 0


if __name__ == "__main__":
    sys.exit(main())
