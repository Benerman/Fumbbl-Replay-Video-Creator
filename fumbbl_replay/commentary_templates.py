"""Local, deterministic commentary lines from the structured pivotal-play data.

No LLM, no install, no network. Each play kind has a small pool of
templates; we pick by `play_index % len(pool)` so the output is
repeatable and consecutive plays of the same kind get different
phrasings. The voice is "1990s sports anchor crossed with pub
football pundit" - intentionally a bit theatrical and dry.

Templates use Python .format() with these keys (any missing key
falls back to a sensible string so the template doesn't blow up):

  scorer / victim / actor   - player_name (or "a {team} player")
  inflicter                 - inflicter_name (casualties only)
  team                      - team_name
  opp                       - against_team
  injury                    - injury_label (e.g. "Head Injury (-AV)")

`tags` on the PivotalPlay choose which pool we read from, so a
game_winning TD never gets a "scored a touchdown" line and a
foul-induced kill never gets a regular block line.
"""

from __future__ import annotations

from typing import Any

from .analyzer import MatchAnalysis, PivotalPlay


# Each pool is a list of single-sentence templates. The renderer picks
# `pool[play_index % len(pool)]` so adjacent plays of the same kind get
# different openers.

_TD_GAME_WINNING = [
    "{scorer} crashes over for the game-winner — that's the dagger!",
    "{scorer} punches it in to break the deadlock; {team} take the lead and don't give it back.",
    "Game over — {scorer} dives over the line and {team} have the win.",
    "{scorer} carries it across for {team} — the decisive score.",
]
_TD_TYING = [
    "{scorer} levels it with a clutch dive into the endzone.",
    "Drama! {scorer} ties it up for {team} — back to square one.",
    "{scorer} drags {team} back into it with a stretch over the line.",
    "All square again — {scorer} crashes through to equalise.",
]
_TD_COMEBACK = [
    "{scorer} answers back for {team} — game's still on.",
    "{scorer} cuts the deficit, {team} are stirring.",
    "Big response from {team}: {scorer} barrels in for the score.",
    "{scorer} powers through, {team} aren't done yet.",
]
_TD_REGULAR = [
    "{scorer} romps in for {team}.",
    "Six points for {team} as {scorer} crosses the line.",
    "{scorer} skips through the line and slides over for the score.",
    "{scorer} grabs paydirt; {team} are on the board.",
    "{scorer} strolls in — easy as you like for {team}.",
]

_KILL_BLOCK = [
    "{inflicter} sends {victim} off in a stretcher — that one's not getting back up.",
    "Lights out for {victim}; {inflicter} wasn't here to make friends.",
    "{victim} is DEAD — {inflicter} finishes the job in the mud.",
    "{inflicter} flattens {victim} for good — {team} are a player short for the season.",
    "{inflicter} cracks {victim} into the next life; the apothecary just shrugs.",
]
_KILL_FOUL = [
    "Sneaky boot from {inflicter} — {victim} is dead, and definitely intentional.",
    "{inflicter} stomps on {victim} when the ref isn't looking — that's a kill.",
    "Dirty work from {inflicter}: {victim} won't be back, in this game or ever.",
]
_KILL_CROWD = [
    "{victim} surfs off the pitch and the crowd finishes the job — that's a kill.",
    "Into the stands and never coming out — {victim} is done.",
]

_SI_BLOCK = [
    "{inflicter} cracks {victim} hard — {injury}, ouch.",
    "{victim} crumples; {inflicter} did the damage, {injury}.",
    "{inflicter} puts {victim} on the shelf — {injury}.",
    "{victim} won't be the same after that one — {inflicter} delivered {injury}.",
]
_SI_FOUL = [
    "Boot to the head from {inflicter} — {victim} takes {injury}, no one saw a thing.",
    "{inflicter} fouls {victim} into the bench — {injury}, season's done.",
]
_SI_CROWD = [
    "{victim} hits the rail hard, {injury} — the fans are not gentle.",
    "Tossed into the crowd; {victim} comes back with {injury}.",
]

_BH_BLOCK = [
    "{inflicter} flattens {victim} — out cold for the half.",
    "{victim} eats canvas; {inflicter} put the lights out.",
    "{inflicter} sits {victim} down — KO box for that one.",
    "{victim} goes down hard, {inflicter} stands over him.",
]
_BH_FOUL = [
    "{inflicter} sneaks in a boot — {victim} is knocked silly.",
]
_BH_CROWD = [
    "{victim} crowd-surfs into a knock-out — the fans are merciless.",
]

_DOUBLE_SKULL = [
    "{actor} swings, rolls double skulls, and faceplants in front of everyone.",
    "Disaster for {team}: {actor} rolls a double skull and the turn ends right there.",
    "{actor} commits to the block — and immediately commits to the floor. Turn over.",
    "Two skulls for {actor} — that's the kind of roll coaches scream at.",
]
_DOUBLE_SKULL_STREAK = [
    "{actor} double-skulls AGAIN — {team} just can't catch a break with the dice.",
    "Yet ANOTHER double skull for {team} — {actor} adds to the snake-eyes pile.",
    "{actor} rolls double skulls, because of course they did — what a day for {team}.",
]
_TRIPLE_SKULL = [
    "TRIPLE skulls?! {actor} has done absolutely nothing useful — turn over.",
    "Three skulls for {actor} — the kind of roll that ends turns and reputations.",
    "Triple skulls. {actor} stares at the dice. The dice stare back.",
]
_CLUTCH_FAIL_NO_WIN = [
    "{actor} fumbles the pickup with everything on the line — game slips away for {team}.",
    "Pickup goes wrong for {actor} in the dying turns; {team} won't be celebrating tonight.",
    "Heartbreak: {actor} drops it near the endzone and the chance is gone.",
]
_CLUTCH_FAIL = [
    "{actor} fumbles the pickup near the endzone — that one stings.",
    "{actor} drops the ball with the line in sight; coach's hat hits the dirt.",
]
_SELF_KILL = [
    "{actor} trips over their own feet and never gets up — what a way to go.",
    "{actor} self-destructs going for it; the pitch ate one without anyone touching them.",
    "Solo cas for {actor}: rolled a 1, broke armour, broke for good.",
]
_INTERCEPTION = [
    "{actor} plucks the ball out of the air — that's the takeaway.",
    "Pick! {actor} reads the throw and snatches it for {team}.",
    "{actor} steps in front of the pass — turnover, just like that.",
]


def render_template_lines(analysis: MatchAnalysis) -> dict[int, str]:
    """Produce {play_index -> commentary line} purely from the analyzer
    output. Deterministic: the same analysis always renders the same
    lines."""
    out: dict[int, str] = {}
    for i, p in enumerate(analysis.pivotal, 1):
        pool = _pool_for(p)
        if not pool:
            continue
        template = pool[i % len(pool)]
        out[i] = template.format(**_vars(p))
    return out


def _pool_for(p: PivotalPlay) -> list[str]:
    tags = p.tags or []
    if p.kind == "touchdown":
        if "game_winning" in tags:
            return _TD_GAME_WINNING
        if "tying" in tags:
            return _TD_TYING
        if "comeback" in tags:
            return _TD_COMEBACK
        return _TD_REGULAR
    if p.kind == "casualty":
        detail = (p.detail or "").lower()
        reason = (p.reason or "").lower()
        if detail == "rip":
            if reason == "fouled":
                return _KILL_FOUL
            if reason == "crowdpushed":
                return _KILL_CROWD
            return _KILL_BLOCK
        if detail == "si":
            if reason == "fouled":
                return _SI_FOUL
            if reason == "crowdpushed":
                return _SI_CROWD
            return _SI_BLOCK
        if detail == "bh":
            if reason == "fouled":
                return _BH_FOUL
            if reason == "crowdpushed":
                return _BH_CROWD
            return _BH_BLOCK
    if p.kind == "triple_skull":
        return _TRIPLE_SKULL
    if p.kind == "double_skull":
        return _DOUBLE_SKULL_STREAK if "snake_eyes_streak" in tags else _DOUBLE_SKULL
    if p.kind == "clutch_fail":
        return _CLUTCH_FAIL_NO_WIN if "no_win" in tags else _CLUTCH_FAIL
    if p.kind == "self_kill":
        return _SELF_KILL
    if p.kind == "interception":
        return _INTERCEPTION
    return []


def _vars(p: PivotalPlay) -> dict[str, Any]:
    """Build the format-substitution dict for one play, with sensible
    fallbacks so templates never blow up on missing names."""
    actor = p.player_name or f"a {p.team_name} player"
    inflicter = p.inflicter_name or "the attacker"
    return {
        "scorer": actor,
        "victim": actor,
        "actor": actor,
        "inflicter": inflicter,
        "team": p.team_name,
        "opp": p.against_team,
        "injury": p.injury_label or "a nasty knock",
    }
