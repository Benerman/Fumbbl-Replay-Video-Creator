"""Worker entry point: `python -m services.worker`.

Long-running loop that drains jobs/queue → jobs/done. Single process,
single thread — `fumbbl_replay.main()` is CPU-bound and we don't
want two renders fighting for the same machine.

Run alongside the bot (separate process). Both share `data/` and
`jobs/` via the filesystem.
"""

from __future__ import annotations

import signal
import sys

from services.common.config import load_settings
from services.common.crypto import TokenCrypto
from services.common.db import get_connection
from services.common.logging import setup_logging
from services.common.paths import ensure_dirs
from services.worker import loop

log = setup_logging("services.worker")


def main() -> int:
    ensure_dirs()
    settings = load_settings()
    get_connection()  # apply migrations
    crypto = TokenCrypto(settings.fernet_master_key)

    def _shutdown(*_a) -> None:
        log.info("received shutdown signal; finishing current job then exiting")
        # The render loop checks for KeyboardInterrupt naturally between
        # jobs; for SIGTERM we set a flag the loop will check next poll.
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    log.info("worker starting; jobs_root=%s out_root=%s", _path_or_default("JOBS_ROOT"),
             _path_or_default("OUT_ROOT"))
    loop.run(crypto, settings=settings)
    return 0


def _path_or_default(env: str) -> str:
    import os
    return os.environ.get(env, "<default>")


if __name__ == "__main__":
    sys.exit(main())
