"""Admin CLI for the Signalweek curated-digest pipeline.

Operator entry point for the two things the pipeline needs a human for:
managing the curated source registry and stepping the weekly issue
through its ``build → verify → publish`` lifecycle (with an ``issue hold``
escape hatch for retracting an issue after publish).

The CLI is a thin ``argparse`` wrapper over modules that already exist —
:mod:`signalweek.sources` for the registry, :mod:`signalweek.digest.builder`
for build, and :mod:`signalweek.digest.verify` for link-liveness. Nothing
that belongs in a library lives here.

Usage::

    python -m signalweek.cli sources add --url URL --kind rss \\
        --category models [--name NAME]
    python -m signalweek.cli sources list
    python -m signalweek.cli sources disable --url URL

    python -m signalweek.cli issue build   [--week YYYY-MM-DD]
    python -m signalweek.cli issue verify  (--week YYYY-MM-DD | --issue-id ID)
    python -m signalweek.cli issue publish (--week YYYY-MM-DD | --issue-id ID)
    python -m signalweek.cli issue hold    (--week YYYY-MM-DD | --issue-id ID)

Per-subscriber commands (accounts, per-user sources, send/email) are
intentionally absent: the product is a public curated publication with no
user accounts, and email delivery is deferred to a later stage.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import TextIO

from sqlalchemy import select
from sqlalchemy.engine import Connection, Engine

from signalweek.db.session import create_db_engine, get_database_url
from signalweek.digest.builder import IssueAlreadyExistsError, build_issue
from signalweek.digest.verify import verify_issue
from signalweek.sources import (
    CATEGORY_HINTS,
    SOURCE_KINDS,
    SourceSpec,
    issues_table,
    sources_table,
    upsert_sources,
)

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_NOT_FOUND = 3
EXIT_CONFLICT = 4


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def main(
    argv: Sequence[str] | None = None,
    *,
    engine: Engine | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    now: datetime | None = None,
) -> int:
    """Run the CLI. Returns a shell exit code.

    ``engine`` lets tests bind the CLI to an in-memory database. ``now`` lets
    tests pin the wall clock the build/publish commands read from — production
    callers pass ``None`` and get :func:`datetime.now` at UTC.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr

    handler = getattr(args, "_handler", None)
    if handler is None:  # pragma: no cover — argparse prints help and exits 2
        parser.print_help(err)
        return EXIT_USAGE

    owned_engine: Engine | None = None
    if engine is None:
        owned_engine = create_db_engine(get_database_url())
        active_engine = owned_engine
    else:
        active_engine = engine

    try:
        with active_engine.begin() as conn:
            return handler(args, conn, out, err, _now_or_default(now))
    finally:
        if owned_engine is not None:
            owned_engine.dispose()


# ---------------------------------------------------------------------------
# argparse wiring
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="signalweek",
        description="Signalweek admin CLI (curated-digest pipeline).",
    )
    subparsers = parser.add_subparsers(dest="group", required=True)

    _register_sources(subparsers)
    _register_issue(subparsers)
    return parser


def _register_sources(subparsers: argparse._SubParsersAction) -> None:
    sources = subparsers.add_parser(
        "sources",
        help="Manage the curated source registry.",
    )
    sources_sub = sources.add_subparsers(dest="command", required=True)

    add = sources_sub.add_parser("add", help="Insert or update one source.")
    add.add_argument("--url", required=True, help="Public feed URL.")
    add.add_argument(
        "--kind",
        required=True,
        choices=sorted(SOURCE_KINDS),
        help="Fetcher family.",
    )
    add.add_argument(
        "--category",
        required=True,
        choices=sorted(CATEGORY_HINTS),
        help="Category hint (one of the five digest sections).",
    )
    add.add_argument("--name", default=None, help="Human-readable label (optional).")
    add.set_defaults(_handler=_cmd_sources_add)

    listing = sources_sub.add_parser("list", help="Print every source in the registry.")
    listing.set_defaults(_handler=_cmd_sources_list)

    disable = sources_sub.add_parser(
        "disable",
        help="Mark a source inactive so the ingest tick skips it.",
    )
    disable.add_argument("--url", required=True, help="Public feed URL to disable.")
    disable.set_defaults(_handler=_cmd_sources_disable)


def _register_issue(subparsers: argparse._SubParsersAction) -> None:
    issue = subparsers.add_parser(
        "issue",
        help="Step the weekly issue through build/verify/publish/hold.",
    )
    issue_sub = issue.add_subparsers(dest="command", required=True)

    build = issue_sub.add_parser("build", help="Build the issue for a given week.")
    build.add_argument(
        "--week",
        default=None,
        type=_parse_iso_date,
        help="Monday of the ISO week to build (YYYY-MM-DD). Defaults to this week.",
    )
    build.set_defaults(_handler=_cmd_issue_build)

    verify = issue_sub.add_parser(
        "verify",
        help="Run the link-liveness verifier on an existing issue.",
    )
    _add_issue_selector(verify)
    verify.set_defaults(_handler=_cmd_issue_verify)

    publish = issue_sub.add_parser(
        "publish",
        help="Mark an existing issue as published (releases a held issue).",
    )
    _add_issue_selector(publish)
    publish.set_defaults(_handler=_cmd_issue_publish)

    hold = issue_sub.add_parser(
        "hold",
        help="Mark an existing issue as held (retracts a published issue).",
    )
    _add_issue_selector(hold)
    hold.set_defaults(_handler=_cmd_issue_hold)


def _add_issue_selector(parser: argparse.ArgumentParser) -> None:
    """Attach the ``--week``/``--issue-id`` mutually exclusive selector."""
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--week",
        type=_parse_iso_date,
        help="Monday of the ISO week the issue covers (YYYY-MM-DD).",
    )
    group.add_argument(
        "--issue-id",
        type=int,
        help="Numeric ``issues.id`` — useful when the week has been rebuilt.",
    )


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------


def _cmd_sources_add(
    args: argparse.Namespace,
    conn: Connection,
    out: TextIO,
    err: TextIO,
    _now: datetime,
) -> int:
    spec = SourceSpec(
        url=args.url.strip(),
        kind=args.kind,
        category_hint=args.category,
        name=args.name.strip() if isinstance(args.name, str) else None,
    )
    result = upsert_sources(conn, [spec])
    if result.inserted:
        print(f"added source {spec.url} ({spec.kind}, {spec.category_hint})", file=out)
    elif result.updated:
        print(f"updated source {spec.url} ({spec.kind}, {spec.category_hint})", file=out)
    else:
        print(f"unchanged source {spec.url} ({spec.kind}, {spec.category_hint})", file=out)
    return EXIT_OK


def _cmd_sources_list(
    _args: argparse.Namespace,
    conn: Connection,
    out: TextIO,
    _err: TextIO,
    _now: datetime,
) -> int:
    rows = conn.execute(
        select(
            sources_table.c.id,
            sources_table.c.url,
            sources_table.c.kind,
            sources_table.c.category_hint,
            sources_table.c.active,
        ).order_by(sources_table.c.id.asc())
    ).all()
    if not rows:
        print("no sources registered", file=out)
        return EXIT_OK
    for row in rows:
        state = "active" if bool(row.active) else "inactive"
        hint = row.category_hint or "-"
        print(f"{int(row.id):>4}  {state:<8}  {row.kind:<9}  {hint:<16}  {row.url}", file=out)
    return EXIT_OK


def _cmd_sources_disable(
    args: argparse.Namespace,
    conn: Connection,
    out: TextIO,
    err: TextIO,
    _now: datetime,
) -> int:
    url = args.url.strip()
    row = conn.execute(select(sources_table.c.id).where(sources_table.c.url == url)).first()
    if row is None:
        print(f"no source with url {url}", file=err)
        return EXIT_NOT_FOUND
    conn.execute(
        sources_table.update().where(sources_table.c.id == int(row.id)).values(active=False)
    )
    print(f"disabled source {url}", file=out)
    return EXIT_OK


def _cmd_issue_build(
    args: argparse.Namespace,
    conn: Connection,
    out: TextIO,
    err: TextIO,
    now: datetime,
) -> int:
    try:
        result = build_issue(conn, now=now, week_of=args.week)
    except IssueAlreadyExistsError as exc:
        print(str(exc), file=err)
        return EXIT_CONFLICT

    per_cat = ", ".join(f"{cat}={count}" for cat, count in result.items_per_category.items())
    print(
        f"built issue id={result.issue_id} week_of={result.week_of.isoformat()} "
        f"status={result.status} items={result.total_items} "
        f"rejected_by_dedup={result.rejected_by_dedup}",
        file=out,
    )
    if per_cat:
        print(f"  per_category: {per_cat}", file=out)
    return EXIT_OK


def _cmd_issue_verify(
    args: argparse.Namespace,
    conn: Connection,
    out: TextIO,
    err: TextIO,
    _now: datetime,
) -> int:
    issue = _resolve_issue(conn, args)
    if issue is None:
        _print_issue_not_found(args, err)
        return EXIT_NOT_FOUND

    result = verify_issue(conn, issue_id=issue.id)
    print(
        f"verified issue id={result.issue_id} checked={result.checked} "
        f"kept={result.kept} dropped={result.dropped} "
        f"drop_rate={result.drop_rate:.2f}",
        file=out,
    )
    for dropped in result.dropped_items:
        code = dropped.status_code if dropped.status_code is not None else "-"
        print(
            f"  dropped item_id={dropped.item_id} status={code} url={dropped.primary_url}",
            file=out,
        )
    return EXIT_OK


def _cmd_issue_publish(
    args: argparse.Namespace,
    conn: Connection,
    out: TextIO,
    err: TextIO,
    now: datetime,
) -> int:
    issue = _resolve_issue(conn, args)
    if issue is None:
        _print_issue_not_found(args, err)
        return EXIT_NOT_FOUND

    if issue.status == "published":
        print(f"issue id={issue.id} already published", file=out)
        return EXIT_OK

    conn.execute(
        issues_table.update()
        .where(issues_table.c.id == issue.id)
        .values(status="published", published_at=now)
    )
    print(
        f"published issue id={issue.id} week_of={issue.week_of.isoformat()} "
        f"published_at={now.isoformat()}",
        file=out,
    )
    return EXIT_OK


def _cmd_issue_hold(
    args: argparse.Namespace,
    conn: Connection,
    out: TextIO,
    err: TextIO,
    _now: datetime,
) -> int:
    issue = _resolve_issue(conn, args)
    if issue is None:
        _print_issue_not_found(args, err)
        return EXIT_NOT_FOUND

    if issue.status == "held":
        print(f"issue id={issue.id} already held", file=out)
        return EXIT_OK

    conn.execute(
        issues_table.update()
        .where(issues_table.c.id == issue.id)
        .values(status="held", published_at=None)
    )
    print(
        f"held issue id={issue.id} week_of={issue.week_of.isoformat()} "
        f"(previous status={issue.status})",
        file=out,
    )
    return EXIT_OK


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _IssueRow:
    id: int
    week_of: date
    status: str


def _resolve_issue(conn: Connection, args: argparse.Namespace) -> _IssueRow | None:
    stmt = select(issues_table.c.id, issues_table.c.week_of, issues_table.c.status)
    issue_id = getattr(args, "issue_id", None)
    if issue_id is not None:
        stmt = stmt.where(issues_table.c.id == int(issue_id))
    else:
        stmt = stmt.where(issues_table.c.week_of == args.week)
    row = conn.execute(stmt).first()
    if row is None:
        return None
    return _IssueRow(id=int(row.id), week_of=row.week_of, status=row.status)


def _print_issue_not_found(args: argparse.Namespace, err: TextIO) -> None:
    issue_id = getattr(args, "issue_id", None)
    if issue_id is not None:
        print(f"no issue with id={issue_id}", file=err)
    else:
        print(f"no issue for week_of={args.week.isoformat()}", file=err)


def _parse_iso_date(raw: str) -> date:
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD, got {raw!r}") from exc


def _now_or_default(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(UTC)
    if now.tzinfo is None:
        return now.replace(tzinfo=UTC)
    return now


if __name__ == "__main__":  # pragma: no cover — invoked via `python -m`.
    raise SystemExit(main())
