# FUMBBL Replay Video Creator

A toolchain for turning a [FUMBBL](https://fumbbl.com) Blood Bowl match
into a 3-5 minute whimsical highlight reel with retro pixel-art
visuals, AI-written commentary, TTS narration, chiptune SFX, and stat
cards.

## Current state: pivotal-play analyzer

A CLI that takes a FUMBBL replay reference and prints the plays that
mattered most - with player names, half, and turn number.

```
$ python -m fumbbl_replay 1901135

  #1901135 (2007-10-28, Ranked) Neck Snappers [Orc, Ballcrusher] 1 - 2 Torcy United [Orc, Tathar]
  Winner: Torcy United (by 1)
  ...
  Pivotal plays (6, from replay event log):
     1. [1.00] Skurrrk-Gnash (Neck Snappers) scored a touchdown (turn 8, half 1)
     2. [0.50] Gozzax (Neck Snappers) was seriously injured (turn 1, half 1) - Head Injury (-AV)
     3. [0.50] Jón Hávarrs (Torcy United) was seriously injured (turn 2, half 2) - Serious Injury (NI)
     ...
```

`<replay-ref>` accepts:
* a bare match id, e.g. `1901135`
* a FUMBBL replay URL, e.g. `https://fumbbl.com/ffblive.jnlp?replay=1901135`
* a path to a local saved `.jnlp` file

### How it gets the event log

There are two FUMBBL data sources we use:

1. `GET /api/match/get/{id}` returns the final scoreboard, casualties,
   coaches, division, and team metadata. No event log, no auth.
2. `GET /api/replay/get/{replayId}/gz` returns the full game log as a
   gzipped JSON. Same per-turn deltas the official FFB Java client
   sees over its websocket on port 22223, but as plain HTTP, so no
   firewalled-port surprises. (The Python `fumbbl_replays` package by
   gsverhoeven uses the same endpoint.)

The replay's `gameLog.commandArray` is a stream of `serverModelSync`
commands. Each command's `modelChangeArray` carries small typed
deltas; we walk them, hold onto sticky state (current half, per-team
turn number), and emit a typed event whenever a counter changes
(`teamResultSetScore` -> TD, `teamResultSetRipSuffered` -> kill,
`teamResultSetSeriousInjurySuffered` -> SI, `teamResultSetBadlyHurtSuffered`
-> BH). The scorer's playerId rides along in the same command on a
companion `playerResultSetTouchdowns`; the casualty victim's
playerId rides on `playerResultSetSeriousInjury`. We resolve those
to player names via the replay's *own* in-game roster (under
`game.teamHome.playerArray` and `game.teamAway.playerArray`) - the
persistent player ids from `/api/team` would not have matched.

### Usage

```bash
pip install -r requirements.txt

# Text report
python -m fumbbl_replay 1901135

# JSON for downstream tooling
python -m fumbbl_replay 1901135 --json

# Save the raw gzipped replay alongside the report
python -m fumbbl_replay 1901135 --dump-replay out/1901135.json.gz

# Skip the replay step, use just summary totals (no player names, no turn)
python -m fumbbl_replay 1901135 --no-replay
```

### Module layout

```
fumbbl_replay/
  __main__.py     - CLI
  jnlp_loader.py  - resolve URL / .jnlp file / bare id -> match id
  fumbbl_api.py   - HTTP client: /api/match, /api/team, /api/replay/.../gz
  events.py       - parse gameLog.commandArray -> typed event timeline + in-game roster
  analyzer.py     - score pivotal plays; fall back to summary totals if no events
```

## Roadmap

| Stage                                                                   | Status |
|-------------------------------------------------------------------------|--------|
| Resolve replay reference (URL / file / id) to a match id                | done   |
| Fetch the gzipped replay over HTTP                                       | done   |
| Parse server commands into a typed event timeline                       | done   |
| Pivotal plays with player names + half + turn                            | done   |
| Match summary fallback analyzer                                          | done   |
| Render stylized pixel-art tableaux per play                              | todo   |
| LLM commentary script + TTS narration                                    | todo   |
| ffmpeg compose final mp4                                                 | todo   |

## License

See `LICENSE`.
