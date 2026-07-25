"""Command-line entry point.

Exposed via the ``signalweek`` console script (see ``pyproject.toml``). Only
one subcommand ships today:

* ``signalweek run-week [--week-start YYYY-MM-DD]`` — materialize a week's
  digests for every active user, invoking the same code path the APScheduler
  cron job uses. Useful for manual reruns and local development.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from datetime import date

from signalweek.scheduler import run_weekly_job


def _parse_monday(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid ISO date: {value!r}") from exc
    if parsed.weekday() != 0:
        raise argparse.ArgumentTypeError(
            f"{value!r} is not a Monday; week windows must begin on Monday"
        )
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="signalweek", description="Signalweek admin CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run_week = subparsers.add_parser(
        "run-week",
        help="Materialize a week's digest for every active user.",
    )
    run_week.add_argument(
        "--week-start",
        type=_parse_monday,
        default=None,
        help=(
            "Monday (ISO date) starting the week to materialize. "
            "Defaults to the Monday of the just-completed week."
        ),
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    runner: Callable[..., list[int]] = run_weekly_job,
) -> int:
    """Parse ``argv`` and dispatch to the requested subcommand.

    ``runner`` is injectable so tests can exercise argument parsing without
    touching the database.
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "run-week":
        created = runner(week_start=args.week_start)
        label = args.week_start.isoformat() if args.week_start else "previous week"
        print(f"Materialized {len(created)} digest row(s) for {label}.")
        return 0
    parser.error(f"unknown command: {args.command}")
    return 2  # unreachable — argparse.error raises SystemExit


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
