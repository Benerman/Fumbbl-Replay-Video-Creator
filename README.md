# FUMBBL Replay Video Creator

A toolchain for turning a [FUMBBL](https://fumbbl.com) Blood Bowl match
into a 3-5 minute whimsical highlight reel with retro pixel-art
visuals, AI-written commentary, TTS narration, chiptune SFX, and stat
cards.

## Current state: pivotal-play analyzer

The first piece is a CLI that, given a FUMBBL match id, prints the
plays that mattered most plus the assets we have to draw with (team
logos, player portraits):

```
$ python -m fumbbl_replay 4700555

  #4700555 (2026-05-15, League) Karag Dum Doomkillers [Chaos Renegade, TheRedoubt] 1 - 0 Norse 10 From Averone [Norse, Mully]
  ---------------------------------------------------------------------------------------------------------------------------
  Winner: Karag Dum Doomkillers (by 1)

  Karag Dum Doomkillers (Chaos Renegade, coach TheRedoubt) - TV 1440k
     logo: https://fumbbl.com/i/752241
     roster: 13 players
     casualties suffered: BH=1 SI=1 RIP=0

  Norse 10 From Averone (Norse, coach Mully) - TV 1350k
     roster: 12 players
     casualties suffered: BH=1 SI=2 RIP=0

  Pivotal plays (6):
     1. [1.00] Karag Dum Doomkillers scored a touchdown
     2. [0.50] Karag Dum Doomkillers had a player seriously injured
     3. [0.50] Norse 10 From Averone had a player seriously injured
     4. [0.50] Norse 10 From Averone had a player seriously injured
     5. [0.20] Karag Dum Doomkillers had a player knocked out
     6. [0.20] Norse 10 From Averone had a player knocked out
```

`--json` mode emits the full structured analysis (each pivotal play,
each player on each team with portrait URLs, etc.) for downstream tools.

### How it works

1. **`fumbbl_api.py`** wraps the two endpoints we need:
   - `GET /api/match/get/{id}` for the match summary (teams, score, casualties)
   - `GET /api/team/get/{id}` for the team roster + bio (logo, players)
   - `image_url(id)` builds the `https://fumbbl.com/i/{id}` asset URL
     that serves logos and portraits as PNGs.
2. **`analyzer.py`** scores plays by win-probability impact:
   touchdowns at 1.0, kills (RIP) at 0.8, serious injuries at 0.5,
   knock-outs at 0.2. It returns a `MatchAnalysis` containing both
   teams (with logo/portrait URLs) and the ranked play list.

### What we know and don't know

We **do** have, per match, from plain HTTP:
- Final score, division, date
- Both teams' name, race, coach, team value
- Both teams' casualty box-score (BH/SI/RIP counts)
- Both teams' full current roster, each player's name, number,
  position, skills, and a portrait image URL
- Team logo image URL

We **don't** have, via plain HTTP:
- Per-turn pitch positions
- Who scored each touchdown
- Who suffered each casualty (the roster gives current state, not the
  per-match snapshot)
- Dice rolls

That richer event log lives behind the FFB websocket on port 22223
(not currently used). So today's analyzer ranks plays at the
team level, not the player level.

## Roadmap

| Stage                                       | Status  |
|---------------------------------------------|---------|
| Match id input, summary + roster fetch      | done    |
| Rank pivotal plays from team-level data     | done    |
| Surface logo + portrait asset URLs          | done    |
| Pull per-turn event log via FFB websocket   | todo    |
| Render stylized pitch tableaux per play     | todo    |
| LLM commentary script + TTS narration       | todo    |
| ffmpeg compose final mp4                    | todo    |

## Install / run

```bash
pip install -r requirements.txt
python -m fumbbl_replay 4700555
python -m fumbbl_replay 4700555 --json
python -m fumbbl_replay 4700555 --no-rosters   # skip the two roster GETs
```

## License

See `LICENSE`.
