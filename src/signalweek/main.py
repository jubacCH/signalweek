"""ASGI entry point used by the Dockerfile and ``uvicorn signalweek.main:app``."""

from __future__ import annotations

import logging
import os
import sys

from signalweek.web import create_app


def configure_logging() -> None:
    """Send app and scheduler logs to stdout so ``docker compose logs`` sees them.

    Uvicorn only configures its own loggers; without a root handler every
    ``signalweek.*`` INFO line (ingest ticks, weekly runs) was dropped.
    """
    logging.basicConfig(
        level=os.environ.get("SIGNALWEEK_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )


configure_logging()
app = create_app()
