"""Validate + sanitize the match-ref a Discord user types.

Defence in depth: `fumbbl_replay.jnlp_loader.resolve` is the canonical
parser and would catch bad input on its own, but we never want to
hand-arbitrary-input strings to it. The sanitizer enforces a strict
char allowlist + URL pattern BEFORE any parsing happens.

We also never shell out — `fumbbl_replay.main()` is called as a
Python function with an `argv` list — so injection into a shell is
not in our threat model. The sanitizer is about giving the user
helpful feedback fast, not about preventing RCE.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from fumbbl_replay.jnlp_loader import Resolved, resolve

MAX_REF_LEN = 256

_ALLOWED_CHARS = re.compile(r"^[A-Za-z0-9:/?=._+\-&]+$")

_BARE_ID = re.compile(r"^\d+$")
_FUMBBL_URL = re.compile(
    r"^https?://(?:www\.)?fumbbl\.com/"
    r"(?:p/match|ffblive\.jnlp)"
    r"\?[A-Za-z0-9=&._-]{1,200}$"
)


@dataclass
class SanitizeResult:
    ok: bool
    cleaned: str = ""
    resolved: Resolved | None = None
    reason: str = ""

    @classmethod
    def deny(cls, reason: str) -> "SanitizeResult":
        return cls(ok=False, reason=reason)


def sanitize_match_ref(raw: str) -> SanitizeResult:
    """Returns a SanitizeResult.

    On success, `cleaned` is the trimmed string ready to pass to the
    worker and `resolved` is the parsed `(match_id, replay_id)`. On
    failure, `reason` is a user-facing explanation safe to send back
    as an ephemeral Discord reply.
    """
    if not isinstance(raw, str):
        return SanitizeResult.deny("Match reference must be a string.")

    cleaned = raw.strip()

    if not cleaned:
        return SanitizeResult.deny("Match reference is empty.")

    if len(cleaned) > MAX_REF_LEN:
        return SanitizeResult.deny(
            f"Match reference is too long (max {MAX_REF_LEN} characters)."
        )

    if not _ALLOWED_CHARS.match(cleaned):
        return SanitizeResult.deny(
            "Match reference contains characters that aren't allowed. "
            "Use a bare numeric match ID or a https://fumbbl.com/... URL."
        )

    if not (_BARE_ID.match(cleaned) or _FUMBBL_URL.match(cleaned)):
        return SanitizeResult.deny(
            "Match reference must be a bare numeric match ID (e.g. `4700552`) "
            "or a `https://fumbbl.com/p/match?id=…` / "
            "`https://fumbbl.com/ffblive.jnlp?replay=…` URL."
        )

    # Defence in depth: even though pattern blocks it, double-check no
    # local-path slips through.
    if cleaned.startswith(("/", ".", "~")):
        return SanitizeResult.deny("Local filesystem paths aren't accepted.")

    # Try the upstream resolver (pure for bare-id and ?id=/?replay= URLs).
    try:
        resolved = resolve(cleaned)
    except ValueError as e:
        return SanitizeResult.deny(f"Couldn't understand that match reference: {e}")

    return SanitizeResult(ok=True, cleaned=cleaned, resolved=resolved)
