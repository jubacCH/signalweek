"""Command-line entry point for signalweek.

Exposes a single ``signalweek`` executable with subcommands. Today only
``schedule`` is wired up here; other subcommands live on sibling feature
branches and merge in alongside their own modules.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
from collections.abc import Sequence

from signalweek.db.session import create_engine, create_session_factory
from signalweek.scheduler import build_scheduler

DEFAULT_DATABASE_URL = "sqlite+aiosqlite:///signalweek.db"


def _database_url() -> str:
    return os.environ.get("SIGNALWEEK_DATABASE_URL", DEFAULT_DATABASE_URL)


async def _run_scheduler_forever(database_url: str) -> None:
    """Start the weekly scheduler and block until the process is signalled."""

    engine = create_engine(database_url)
    session_factory = create_session_factory(engine)
    scheduler = build_scheduler(session_factory)
    scheduler.start()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            # Windows / restricted environments: fall back to KeyboardInterrupt.
            pass

    try:
        await stop.wait()
    finally:
        scheduler.shutdown(wait=False)
        await engine.dispose()


def _cmd_schedule(_: argparse.Namespace) -> int:
    try:
        asyncio.run(_run_scheduler_forever(_database_url()))
    except KeyboardInterrupt:
        pass
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="signalweek")
    subparsers = parser.add_subparsers(dest="command", required=True)

    schedule = subparsers.add_parser(
        "schedule",
        help="Start the weekly scheduler (Mondays 09:00 UTC).",
    )
    schedule.set_defaults(func=_cmd_schedule)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    func = args.func
    result = func(args)
    return int(result) if result is not None else 0
