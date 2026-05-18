"""SQLite schema + helpers for the bot/worker shared state.

One file at `data/app.sqlite3` holds four tables:
  - guild_config: per-server YouTube override (refresh token + channel id)
  - bot_defaults: the operator's default YouTube account (single row)
  - processed_replays: dedup record of every successfully-uploaded match
  - rate_log: append-only history for per-guild rate limiting

The connection is opened with `check_same_thread=False` because the
bot uses asyncio (different cooperating tasks) and we serialize all
writes through a module-level `_lock` to keep SQLite happy.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Iterator

from .paths import SQLITE_PATH, ensure_dirs

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS guild_config (
    guild_id INTEGER PRIMARY KEY,
    yt_refresh_token_encrypted BLOB,
    yt_channel_id TEXT,
    set_by_user_id INTEGER,
    set_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- match_id and replay_id are NOT NULL with sentinel -1 because SQLite
-- treats NULL as distinct from NULL in PRIMARY KEY / UNIQUE, which
-- would let duplicate (guild, match) rows slip through. Helpers below
-- convert None -> -1 on insert and -1 -> None on read.
CREATE TABLE IF NOT EXISTS processed_replays (
    guild_id INTEGER NOT NULL,
    match_id INTEGER NOT NULL DEFAULT -1,
    replay_id INTEGER NOT NULL DEFAULT -1,
    youtube_video_id TEXT NOT NULL,
    youtube_url TEXT NOT NULL,
    -- Short upload columns are nullable: a row exists iff the regular
    -- 16:9 video uploaded successfully, but the 9:16 Short is
    -- best-effort and may have failed independently.
    youtube_short_video_id TEXT,
    youtube_short_url TEXT,
    processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    used_default_yt_creds INTEGER DEFAULT 1,
    PRIMARY KEY (guild_id, match_id, replay_id)
);

CREATE INDEX IF NOT EXISTS processed_replays_match
    ON processed_replays(guild_id, match_id);
CREATE INDEX IF NOT EXISTS processed_replays_replay
    ON processed_replays(guild_id, replay_id);

CREATE TABLE IF NOT EXISTS rate_log (
    guild_id INTEGER NOT NULL,
    user_id  INTEGER NOT NULL,
    issued_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS rate_log_guild_time ON rate_log(guild_id, issued_at);

CREATE TABLE IF NOT EXISTS bot_defaults (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    yt_refresh_token_encrypted BLOB NOT NULL,
    yt_channel_id TEXT
);
"""

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


def get_connection() -> sqlite3.Connection:
    """Lazy-init singleton connection. Safe to call from multiple threads."""
    global _conn
    if _conn is None:
        ensure_dirs()
        _conn = sqlite3.connect(SQLITE_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA foreign_keys = ON")
        _conn.execute("PRAGMA journal_mode = WAL")
        _conn.executescript(_SCHEMA)
        _migrate(_conn)
        _conn.commit()
        log.info("opened sqlite at %s", SQLITE_PATH)
    return _conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Idempotent column additions for forward-compatible schema bumps.

    SQLite's ADD COLUMN is non-destructive but errors if the column
    already exists, so we check pragma_table_info first.
    """
    cur = conn.cursor()
    cols = {row["name"] for row in cur.execute(
        "SELECT name FROM pragma_table_info('processed_replays')"
    )}
    if "youtube_short_video_id" not in cols:
        cur.execute("ALTER TABLE processed_replays ADD COLUMN youtube_short_video_id TEXT")
        log.info("migration: added processed_replays.youtube_short_video_id")
    if "youtube_short_url" not in cols:
        cur.execute("ALTER TABLE processed_replays ADD COLUMN youtube_short_url TEXT")
        log.info("migration: added processed_replays.youtube_short_url")
    cur.close()


@contextmanager
def cursor() -> Iterator[sqlite3.Cursor]:
    """Write-locked cursor. Use for every operation, read or write."""
    conn = get_connection()
    with _lock:
        cur = conn.cursor()
        try:
            yield cur
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()


# ---- guild_config -----------------------------------------------------------

def get_guild_config(guild_id: int) -> sqlite3.Row | None:
    with cursor() as cur:
        cur.execute("SELECT * FROM guild_config WHERE guild_id = ?", (guild_id,))
        return cur.fetchone()


def set_guild_youtube(
    guild_id: int,
    refresh_token_encrypted: bytes,
    yt_channel_id: str | None,
    set_by_user_id: int,
) -> None:
    with cursor() as cur:
        cur.execute(
            """
            INSERT INTO guild_config
                (guild_id, yt_refresh_token_encrypted, yt_channel_id, set_by_user_id)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                yt_refresh_token_encrypted = excluded.yt_refresh_token_encrypted,
                yt_channel_id = excluded.yt_channel_id,
                set_by_user_id = excluded.set_by_user_id,
                set_at = CURRENT_TIMESTAMP
            """,
            (guild_id, refresh_token_encrypted, yt_channel_id, set_by_user_id),
        )


def reset_guild_youtube(guild_id: int) -> None:
    with cursor() as cur:
        cur.execute("DELETE FROM guild_config WHERE guild_id = ?", (guild_id,))


# ---- bot_defaults -----------------------------------------------------------

def get_bot_defaults() -> sqlite3.Row | None:
    with cursor() as cur:
        cur.execute("SELECT * FROM bot_defaults WHERE id = 1")
        return cur.fetchone()


def set_bot_defaults(refresh_token_encrypted: bytes, yt_channel_id: str | None) -> None:
    with cursor() as cur:
        cur.execute(
            """
            INSERT INTO bot_defaults (id, yt_refresh_token_encrypted, yt_channel_id)
            VALUES (1, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                yt_refresh_token_encrypted = excluded.yt_refresh_token_encrypted,
                yt_channel_id = excluded.yt_channel_id
            """,
            (refresh_token_encrypted, yt_channel_id),
        )


# ---- processed_replays ------------------------------------------------------

_MID = -1  # sentinel for "unknown match_id / replay_id"


def find_processed(
    guild_id: int,
    match_id: int | None,
    replay_id: int | None,
) -> sqlite3.Row | None:
    """Look up an existing successful upload for this guild + ref."""
    if match_id is None and replay_id is None:
        return None
    with cursor() as cur:
        cur.execute(
            """
            SELECT * FROM processed_replays
            WHERE guild_id = ?
              AND (match_id = ? OR replay_id = ?)
            ORDER BY processed_at DESC
            LIMIT 1
            """,
            (guild_id, match_id if match_id is not None else _MID,
             replay_id if replay_id is not None else _MID),
        )
        return cur.fetchone()


def record_processed(
    guild_id: int,
    match_id: int | None,
    replay_id: int | None,
    youtube_video_id: str,
    youtube_url: str,
    used_default_creds: bool,
    *,
    youtube_short_video_id: str | None = None,
    youtube_short_url: str | None = None,
) -> None:
    with cursor() as cur:
        cur.execute(
            """
            INSERT OR REPLACE INTO processed_replays
                (guild_id, match_id, replay_id,
                 youtube_video_id, youtube_url,
                 youtube_short_video_id, youtube_short_url,
                 used_default_yt_creds)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (guild_id,
             match_id if match_id is not None else _MID,
             replay_id if replay_id is not None else _MID,
             youtube_video_id, youtube_url,
             youtube_short_video_id, youtube_short_url,
             1 if used_default_creds else 0),
        )


# ---- rate_log ---------------------------------------------------------------

def count_recent_invocations(guild_id: int, window_seconds: int) -> int:
    cutoff = datetime.utcnow() - timedelta(seconds=window_seconds)
    with cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM rate_log WHERE guild_id = ? AND issued_at > ?",
            (guild_id, cutoff.isoformat(sep=" ")),
        )
        return int(cur.fetchone()[0])


def log_invocation(guild_id: int, user_id: int) -> None:
    with cursor() as cur:
        cur.execute(
            "INSERT INTO rate_log (guild_id, user_id) VALUES (?, ?)",
            (guild_id, user_id),
        )


def prune_rate_log(older_than_seconds: int = 3600) -> int:
    cutoff = datetime.utcnow() - timedelta(seconds=older_than_seconds)
    with cursor() as cur:
        cur.execute("DELETE FROM rate_log WHERE issued_at <= ?",
                    (cutoff.isoformat(sep=" "),))
        return cur.rowcount
