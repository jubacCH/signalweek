"""Self-evolving source registry: mine cited domains and promote them.

The pipeline never accepts hand-added sources. New sources are derived from
the news flow itself in two rule-based passes:

1. :func:`mine_cited_domains` walks every ``items`` row of every ``issues``
   row, extracts the domain of ``primary_url`` and of each
   ``extra_source_urls`` entry, and rewrites the ``source_candidates`` table
   with the running counts (``cite_count``, ``distinct_weeks_count``,
   ``first_seen_week``, ``last_seen_week``). Existing promotion state is
   preserved on any row that already exists.
2. :func:`promote_candidates` finds candidates that clear a configurable
   citation and week-coverage threshold, are not already in ``sources``, and
   are not already promoted, then inserts a row per candidate into
   ``sources`` — ``active=True``, ``discovered=True``, with the
   ``first_seen_week`` and ``cite_count`` provenance carried onto the new
   row. The candidate row is flipped to ``promoted=True`` with a link back
   to the new source, so every promotion is auditable after the fact.

Both operations are idempotent: mining fully rebuilds the counts from the
current ``items`` table, and promotion refuses to double-promote a domain
that already appears in ``sources``.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from signalweek.sources import (
    issues_table,
    items_table,
    source_candidates_table,
    sources_table,
)

# A domain must be cited by at least this many distinct items to be eligible
# for promotion. Rule-based, tunable at the call site.
DEFAULT_MIN_CITE_COUNT = 3

# ...and it must have been cited across at least this many distinct weeks —
# a single big week does not on its own qualify a domain as a source.
DEFAULT_MIN_DISTINCT_WEEKS = 2


@dataclass(frozen=True)
class DiscoveryStats:
    """Per-domain aggregation produced by :func:`mine_cited_domains`."""

    domain: str
    first_seen_week: date
    last_seen_week: date
    cite_count: int
    distinct_weeks_count: int


@dataclass
class MiningResult:
    """Outcome of one :func:`mine_cited_domains` run."""

    inserted: int = 0
    updated: int = 0
    unchanged: int = 0

    @property
    def total(self) -> int:
        return self.inserted + self.updated + self.unchanged


@dataclass(frozen=True)
class Promotion:
    """A single promotion event recorded by :func:`promote_candidates`."""

    domain: str
    source_id: int
    candidate_id: int
    first_seen_week: date
    cite_count: int
    distinct_weeks_count: int


@dataclass
class PromotionResult:
    """Outcome of one :func:`promote_candidates` run."""

    promotions: list[Promotion] = field(default_factory=list)
    skipped_existing_source: int = 0
    skipped_below_threshold: int = 0

    @property
    def promoted(self) -> int:
        return len(self.promotions)


def mine_cited_domains(bind: Session | Connection) -> MiningResult:
    """Recompute ``source_candidates`` from the current ``items`` table.

    Every ``items`` row contributes at most one citation per domain — the
    same domain appearing on both ``primary_url`` and ``extra_source_urls``
    of one item still counts once. Weeks are taken from the item's issue's
    ``week_of``. Rows in ``source_candidates`` that no longer have any
    citation coverage are removed; existing rows keep their ``promoted``,
    ``promoted_at``, and ``promoted_source_id`` fields untouched.
    """
    connection = _as_connection(bind)

    aggregated = _aggregate_citations(connection)

    # Load existing rows once so we can decide insert vs. update vs. delete
    # in one pass.
    existing_rows = connection.execute(
        select(
            source_candidates_table.c.id,
            source_candidates_table.c.domain,
            source_candidates_table.c.first_seen_week,
            source_candidates_table.c.last_seen_week,
            source_candidates_table.c.cite_count,
            source_candidates_table.c.distinct_weeks_count,
        )
    ).all()
    existing_by_domain = {row.domain: row for row in existing_rows}

    result = MiningResult()

    for domain, stats in aggregated.items():
        existing = existing_by_domain.get(domain)
        if existing is None:
            connection.execute(
                source_candidates_table.insert().values(
                    domain=domain,
                    first_seen_week=stats.first_seen_week,
                    last_seen_week=stats.last_seen_week,
                    cite_count=stats.cite_count,
                    distinct_weeks_count=stats.distinct_weeks_count,
                    promoted=False,
                    promoted_at=None,
                    promoted_source_id=None,
                )
            )
            result.inserted += 1
            continue

        needs_update = (
            existing.first_seen_week != stats.first_seen_week
            or existing.last_seen_week != stats.last_seen_week
            or int(existing.cite_count) != stats.cite_count
            or int(existing.distinct_weeks_count) != stats.distinct_weeks_count
        )
        if needs_update:
            connection.execute(
                source_candidates_table.update()
                .where(source_candidates_table.c.id == int(existing.id))
                .values(
                    first_seen_week=stats.first_seen_week,
                    last_seen_week=stats.last_seen_week,
                    cite_count=stats.cite_count,
                    distinct_weeks_count=stats.distinct_weeks_count,
                )
            )
            result.updated += 1
        else:
            result.unchanged += 1

    # Any candidate row whose domain has vanished from the items table gets
    # dropped — mining fully owns non-promotion fields, so a stale row would
    # only mislead future promotion decisions.
    stale_domains = set(existing_by_domain) - set(aggregated)
    if stale_domains:
        connection.execute(
            source_candidates_table.delete().where(
                source_candidates_table.c.domain.in_(stale_domains)
            )
        )

    return result


def promote_candidates(
    bind: Session | Connection,
    *,
    now: datetime | None = None,
    min_cite_count: int = DEFAULT_MIN_CITE_COUNT,
    min_distinct_weeks: int = DEFAULT_MIN_DISTINCT_WEEKS,
) -> PromotionResult:
    """Promote qualifying candidates into ``sources``.

    A candidate qualifies when its ``cite_count`` is at least
    ``min_cite_count`` **and** its ``distinct_weeks_count`` is at least
    ``min_distinct_weeks``. Domains that are already the URL host of any
    existing ``sources`` row are skipped — they are already covered, so
    re-inserting would create a duplicate registry entry.

    The new source URL is ``https://<domain>/``, ``kind='rss'``,
    ``active=True``, ``discovered=True`` — plus the provenance fields
    ``discovered_first_seen_week`` and ``discovered_cite_count`` copied from
    the candidate. The candidate row is updated with ``promoted=True``,
    ``promoted_at=now``, and ``promoted_source_id`` pointing at the new
    source, so every promotion has an auditable trail.
    """
    connection = _as_connection(bind)
    stamp = _resolve_now(now)

    existing_source_domains = _existing_source_domains(connection)

    candidates = connection.execute(
        select(
            source_candidates_table.c.id,
            source_candidates_table.c.domain,
            source_candidates_table.c.first_seen_week,
            source_candidates_table.c.cite_count,
            source_candidates_table.c.distinct_weeks_count,
        )
        .where(source_candidates_table.c.promoted.is_(False))
        .order_by(
            source_candidates_table.c.cite_count.desc(),
            source_candidates_table.c.domain.asc(),
        )
    ).all()

    result = PromotionResult()

    for row in candidates:
        cite_count = int(row.cite_count)
        distinct_weeks = int(row.distinct_weeks_count)
        if cite_count < min_cite_count or distinct_weeks < min_distinct_weeks:
            result.skipped_below_threshold += 1
            continue
        if row.domain in existing_source_domains:
            result.skipped_existing_source += 1
            continue

        new_source_url = f"https://{row.domain}/"
        inserted = connection.execute(
            sources_table.insert()
            .values(
                url=new_source_url,
                kind="rss",
                category_hint=None,
                active=True,
                discovered=True,
                discovered_first_seen_week=row.first_seen_week,
                discovered_cite_count=cite_count,
            )
            .returning(sources_table.c.id)
        )
        new_source_id = int(inserted.scalar_one())

        connection.execute(
            source_candidates_table.update()
            .where(source_candidates_table.c.id == int(row.id))
            .values(
                promoted=True,
                promoted_at=stamp,
                promoted_source_id=new_source_id,
            )
        )

        result.promotions.append(
            Promotion(
                domain=row.domain,
                source_id=new_source_id,
                candidate_id=int(row.id),
                first_seen_week=row.first_seen_week,
                cite_count=cite_count,
                distinct_weeks_count=distinct_weeks,
            )
        )
        existing_source_domains.add(row.domain)

    return result


def discover_and_promote(
    bind: Session | Connection,
    *,
    now: datetime | None = None,
    min_cite_count: int = DEFAULT_MIN_CITE_COUNT,
    min_distinct_weeks: int = DEFAULT_MIN_DISTINCT_WEEKS,
) -> tuple[MiningResult, PromotionResult]:
    """Mine cited domains, then promote those that clear the threshold.

    Convenience wrapper for the two-step pass a scheduler tick would run
    right after the weekly build.
    """
    mining = mine_cited_domains(bind)
    promotion = promote_candidates(
        bind,
        now=now,
        min_cite_count=min_cite_count,
        min_distinct_weeks=min_distinct_weeks,
    )
    return mining, promotion


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _aggregate_citations(connection: Connection) -> dict[str, DiscoveryStats]:
    rows = connection.execute(
        select(
            items_table.c.primary_url,
            items_table.c.extra_source_urls,
            issues_table.c.week_of,
        ).select_from(items_table.join(issues_table, items_table.c.issue_id == issues_table.c.id))
    ).all()

    per_domain_weeks: dict[str, set[date]] = {}
    per_domain_cites: dict[str, int] = {}
    per_domain_first: dict[str, date] = {}
    per_domain_last: dict[str, date] = {}

    for row in rows:
        week: date = row.week_of
        item_urls = _iter_item_urls(row.primary_url, row.extra_source_urls)
        seen_this_item: set[str] = set()
        for url in item_urls:
            domain = _extract_domain(url)
            if not domain or domain in seen_this_item:
                continue
            seen_this_item.add(domain)

            per_domain_cites[domain] = per_domain_cites.get(domain, 0) + 1
            weeks = per_domain_weeks.setdefault(domain, set())
            weeks.add(week)
            if domain not in per_domain_first or week < per_domain_first[domain]:
                per_domain_first[domain] = week
            if domain not in per_domain_last or week > per_domain_last[domain]:
                per_domain_last[domain] = week

    return {
        domain: DiscoveryStats(
            domain=domain,
            first_seen_week=per_domain_first[domain],
            last_seen_week=per_domain_last[domain],
            cite_count=per_domain_cites[domain],
            distinct_weeks_count=len(per_domain_weeks[domain]),
        )
        for domain in per_domain_cites
    }


def _iter_item_urls(primary_url: str, extras: object) -> Iterable[str]:
    if primary_url:
        yield primary_url
    if isinstance(extras, list):
        for extra in extras:
            if isinstance(extra, str) and extra:
                yield extra


def _existing_source_domains(connection: Connection) -> set[str]:
    rows = connection.execute(select(sources_table.c.url)).all()
    domains: set[str] = set()
    for row in rows:
        domain = _extract_domain(row.url)
        if domain:
            domains.add(domain)
    return domains


def _extract_domain(url: str) -> str:
    if not url:
        return ""
    host = (urlsplit(url).hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def _resolve_now(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(UTC)
    if now.tzinfo is None:
        return now.replace(tzinfo=UTC)
    return now


def _as_connection(bind: Session | Connection) -> Connection:
    if isinstance(bind, Session):
        return bind.connection()
    return bind
