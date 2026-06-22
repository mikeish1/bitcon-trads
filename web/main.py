"""
Dashboard entrypoint: `python -m web.main`.

Co-location pattern (the approved deployment model, architecture §11):
  * This is launched as the `web` child of the existing supervisor
    (`src.run_all`) when RUN_BOTS includes "web", e.g. RUN_BOTS=spot,web. It runs
    in the SAME container as the trading bot and shares the volume-mounted SQLite
    file, which it opens READ-ONLY. One service, one volume, numReplicas=1.
  * It can also be run standalone for local development:
        pip install -r requirements-web.txt
        python -m web.main            # serves on $PORT (default 8080)

Why a separate process (not a thread inside the bot): the trading loop is
synchronous and signal-driven; keeping the async web server in its own process
isolates its failure domain entirely (a web crash can never touch the trader) and
matches how the supervisor already manages sibling bots with restart backoff.
"""
from __future__ import annotations

import os

import uvicorn
from loguru import logger

from src.config import load_config
from web.server import create_app

# Module-level app so `uvicorn web.main:app` also works (e.g. with --workers 1).
app = create_app(load_config())


def main() -> None:
    port = int(os.getenv("PORT", "8080"))
    host = os.getenv("DASHBOARD_HOST", "0.0.0.0")
    logger.info("Starting dashboard on {}:{}", host, port)
    # workers=1: a single async worker is plenty for a read-heavy internal dashboard,
    # and the snapshot sampler must be a singleton (multiple workers would multi-write).
    uvicorn.run(app, host=host, port=port, log_level=os.getenv("LOG_LEVEL", "info").lower(),
                access_log=False)  # we do our own structured request logging


if __name__ == "__main__":
    main()
