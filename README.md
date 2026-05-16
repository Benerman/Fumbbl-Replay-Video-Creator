# FUMBBL Replay Video Creator

A toolchain for turning a [FUMBBL](https://fumbbl.com) Blood Bowl match
into a 3-5 minute whimsical highlight reel with retro pixel-art
visuals, AI-written commentary, TTS narration, chiptune SFX, and stat
cards.

## Current state: replay fetcher

The data foundation: pull a full replay from FUMBBL via the FFB websocket
protocol, the same way the official Java client does it. This is the
only source of the rich per-turn event log (who scored each TD, every
dice roll, every casualty).

### How the protocol works

1. The user gives us a FFB launcher `.jnlp` (URL or file).
2. We parse out `gameId`, `host`, `port`, and `coach`.
3. We open `ws://{host}:{port}/command` and send one message:
   ```json
   {"netCommandId":"clientReplay","gameId":1901135,"replayToCommandNr":0,"coach":"spectator"}
   ```
   `replayToCommandNr: 0` means "no upper bound — send the whole replay."
4. The server streams the entire game log back as a sequence of
   `serverReplay` messages, each carrying a `commandArray` of up to 100
   inner `ServerCommand`s plus a `totalNrOfCommands` and a
   `lastCommand: true|false` flag. We loop until `lastCommand: true`.

Each inner ServerCommand is either a `serverModelSync` (deltas to game
state) or carries a `reportList.reports` containing event-typed
entries (touchdowns, injuries, dice rolls, kickoff results, etc.).

### Usage

```bash
pip install -r requirements.txt

# Fetch the full replay and dump it to NDJSON for inspection.
python -m fumbbl_replay fetch \
    https://fumbbl.com/ffblive.jnlp?replay=1901135 \
    --dump out/1901135.ndjson

# Or from a JNLP you've saved locally:
python -m fumbbl_replay fetch ./replay-1901135.jnlp --dump out/1901135.ndjson

# Quick analysis straight from FUMBBL's match-summary API (no websocket):
python -m fumbbl_replay summary 4700555
```

### Network reachability

The `fetch` subcommand connects to `fumbbl.com:22223`. That port is
firewalled from many cloud sandboxes (including this repo's CI/Codespaces
environments), but is reachable from a normal home/office machine. If you
get a timeout, run from somewhere outside the cloud.

### Module layout

```
fumbbl_replay/
  jnlp_loader.py    - parse JNLP (URL or file) -> JnlpReplayInfo
  ffb_client.py     - speak the FFB ws protocol, return all server messages
  fumbbl_api.py     - the public /api/match + /api/team JSON endpoints
  analyzer.py       - pivotal-play scoring from match summary (current
                       fallback; will be rebuilt on real event data)
  __main__.py       - CLI: `fetch` (websocket) and `summary` (API only)
```

## Roadmap

| Stage                                                                   | Status |
|-------------------------------------------------------------------------|--------|
| Parse JNLP (URL or file) -> connection params                           | done   |
| FFB websocket client: send clientReplay, collect all batches            | done   |
| CLI dump replay to NDJSON                                                | done   |
| Match summary fallback analyzer                                          | done   |
| Parse server commands into a typed event timeline                       | next   |
| Identify pivotal plays with player names + turn numbers                  | next   |
| Render stylized pixel-art tableaux per play                              | todo   |
| LLM commentary script + TTS narration                                    | todo   |
| ffmpeg compose final mp4                                                 | todo   |

## License

See `LICENSE`.
