"""Logging setup shared by the bot and the worker.

One-line per record, ISO timestamp, level, logger name, message. The
docker compose stdout / journald are the canonical sinks — no file
rotation needed.
"""

from __future__ import annotations

import logging
import os


def setup_logging(name: str) -> logging.Logger:
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    # Quiet noisy libraries.
    for noisy in ("urllib3", "googleapiclient.discovery_cache",
                   "discord", "discord.client", "discord.gateway"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return logging.getLogger(name)
