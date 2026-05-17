"""Per-guild rate limit + per-user in-flight tracker.

Per-guild is persistent (SQLite rate_log table) so a bot restart
doesn't reset the bucket. Per-user is in-memory only — if the bot
restarts mid-job the user can re-issue, which is what we'd want
anyway (the worker's idempotency table catches duplicate uploads).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from . import db


@dataclass
class RateLimitDecision:
    allowed: bool
    reason: str = ""

    @classmethod
    def ok(cls) -> "RateLimitDecision":
        return cls(allowed=True)

    @classmethod
    def deny(cls, reason: str) -> "RateLimitDecision":
        return cls(allowed=False, reason=reason)


class UserInFlightTracker:
    """In-memory set of user_ids currently running a job."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._in_flight: set[int] = set()

    def try_claim(self, user_id: int) -> bool:
        with self._lock:
            if user_id in self._in_flight:
                return False
            self._in_flight.add(user_id)
            return True

    def release(self, user_id: int) -> None:
        with self._lock:
            self._in_flight.discard(user_id)

    def is_in_flight(self, user_id: int) -> bool:
        with self._lock:
            return user_id in self._in_flight


def check_guild_rate(guild_id: int, max_per_10min: int) -> RateLimitDecision:
    """Look at rate_log for the last 600 s. Allow or deny."""
    recent = db.count_recent_invocations(guild_id, window_seconds=600)
    if recent >= max_per_10min:
        return RateLimitDecision.deny(
            f"This server already used its {max_per_10min} highlights this "
            "10-minute window. Please wait a bit before trying again."
        )
    return RateLimitDecision.ok()


def record_guild_invocation(guild_id: int, user_id: int) -> None:
    db.log_invocation(guild_id, user_id)
