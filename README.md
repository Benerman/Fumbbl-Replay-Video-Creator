# FUMBBL Replay Video Creator

A toolchain for turning a [FUMBBL](https://fumbbl.com) Blood Bowl match
into a 3-5 minute whimsical highlight reel with retro pixel-art
visuals, AI-written commentary, TTS narration, chiptune SFX, and stat
cards.

## Current state: pivotal-play analyzer

The first piece is a CLI that, given a FUMBBL replay reference, prints
the plays that mattered most:

```
$ python -m fumbbl_replay https://fumbbl.com/ffblive.jnlp?replay=1901135

  #1901135 (2007-10-28, Ranked) Neck Snappers [Orc, Ballcrusher] 1 - 2 Torcy United [Orc, Tathar]
  -----------------------------------------------------------------------------------------
  Winner: Torcy United (by 1)

  Pivotal plays (5):
     1. [1.00] Torcy United scored a touchdown
     2. [1.00] Torcy United scored a touchdown
     3. [1.00] Neck Snappers scored a touchdown
     4. [0.80] Neck Snappers had a player killed
     5. [0.50] Torcy United had a player seriously injured
```

The `<replay-ref>` you pass can be a FUMBBL replay URL, a local
`.jnlp` file path, or a bare game id.

### How it works

1. **`jnlp_loader.py`** - resolves the input to a game id. If you pass
   a URL, it pulls `?replay=N` from the query string (and, where
   useful, also fetches the JNLP to read `-gameId` arguments).
2. **`fumbbl_api.py`** - fetches the match summary from
   `https://fumbbl.com/api/match/get/{id}` (no auth needed).
3. **`analyzer.py`** - scores plays by win-probability impact:
   touchdowns at 1.0, kills (RIP) at 0.8, serious injuries at 0.5,
   knock-outs at 0.2. Returns a ranked list.

### Limits today

The match summary endpoint doesn't include the per-turn event log -
that data lives behind the FFB websocket on port 22223 and isn't
exposed via plain HTTP. So right now the analyzer knows that N TDs
were scored but not when or by whom, and that M casualties happened
but not which player. The output is still useful as a "did this match
matter?" filter and as the skeleton for the highlight reel.

When we add a websocket client (see notes in `jnlp_loader.py`) we'll
augment each `PivotalPlay` with turn number, player names, and the
dice rolls behind each result.

## Roadmap

| Stage                                  | Status  |
|----------------------------------------|---------|
| Resolve replay ref to game id          | done    |
| Fetch + parse match summary            | done    |
| Rank pivotal plays from summary        | done    |
| Pull per-turn event log via FFB ws     | todo    |
| Capture retro visuals per highlight    | todo    |
| LLM commentary script + TTS narration  | todo    |
| ffmpeg compose final mp4               | todo    |

## Install / run

```bash
pip install -r requirements.txt
python -m fumbbl_replay https://fumbbl.com/ffblive.jnlp?replay=1901135
python -m fumbbl_replay 1901135 --json
```

## License

See `LICENSE`.
