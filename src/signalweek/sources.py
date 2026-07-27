"""Static source registry and shared Core-table definitions.

The editorial pipeline pulls from a fixed, checked-in list of feeds rather
than accepting user-added sources. This module owns:

* :data:`CATEGORY_HINTS` and :data:`SOURCE_KINDS` — the closed vocabularies
  the YAML file is validated against.
* :func:`load_sources_yaml` — parse and validate ``sources.yaml``.
* :func:`upsert_sources` / :func:`upsert_sources_from_yaml` — write the
  parsed specs into the ``sources`` table, updating rows in place when the
  URL already exists so re-running the loader is idempotent.
* :data:`sources_table` / :data:`raw_items_table` / :data:`clusters_table` /
  :data:`issues_table` / :data:`items_table` — SQLAlchemy Core tables that
  mirror the columns created by migration ``0003_curated_digest_schema``. The
  ingest, build, and CLI layers all read and write through these Core
  definitions; there is no declarative ORM base for the curated pipeline.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    select,
)
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SOURCES_YAML = REPO_ROOT / "sources.yaml"

CATEGORY_HINTS: frozenset[str] = frozenset(
    {
        "models",
        "lawsuits_policy",
        "funding",
        "research",
        "industry_moves",
    }
)

SOURCE_KINDS: frozenset[str] = frozenset({"rss", "atom", "arxiv_rss"})

sources_metadata = MetaData()

sources_table = Table(
    "sources",
    sources_metadata,
    Column("id", Integer, primary_key=True),
    Column("url", String(2048), nullable=False, unique=True, index=True),
    Column("kind", String(32), nullable=False),
    Column("category_hint", String(64), nullable=True),
    Column("active", Boolean, nullable=False, default=True, server_default="1"),
)

# Raw articles/posts ingested from each source, before clustering/summarization.
# Mirrors the ``raw_items`` table created by migration 0003.
raw_items_table = Table(
    "raw_items",
    sources_metadata,
    Column(
        "id",
        Integer,
        primary_key=True,
    ),
    Column(
        "source_id",
        Integer,
        ForeignKey("sources.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    ),
    Column("url", String(2048), nullable=False),
    Column("canonical_url", String(2048), nullable=False, index=True),
    Column("title", String(1024), nullable=False),
    Column("body", Text, nullable=True),
    Column("fetched_at", DateTime(timezone=True), nullable=False),
    Column("first_seen_at", DateTime(timezone=True), nullable=False, index=True),
    UniqueConstraint("source_id", "canonical_url", name="uq_raw_items_source_canonical"),
)

# Dedup groups of raw_items that all cover the same story. The clustering pass
# in :mod:`signalweek.ingest.cluster` upserts rows here as it groups incoming
# raw_items — ``primary_url`` and ``canonical_headline`` come from the earliest
# (by ``first_seen_at``) raw_item in the group.
# Mirrors the ``clusters`` table created by migration 0003.
clusters_table = Table(
    "clusters",
    sources_metadata,
    Column("id", Integer, primary_key=True),
    Column("primary_url", String(2048), nullable=False, index=True),
    Column("category", String(64), nullable=False, index=True),
    Column("canonical_headline", String(1024), nullable=False),
)

# One row per weekly issue of the digest. ``status`` moves ``draft`` → ``held``
# (fewer than the minimum item count) or ``draft`` → ``published`` (a full
# issue). ``week_of`` is the Monday of the ISO week the issue covers.
# Mirrors the ``issues`` table created by migration 0003.
issues_table = Table(
    "issues",
    sources_metadata,
    Column("id", Integer, primary_key=True),
    Column("week_of", Date, nullable=False),
    Column("status", String(16), nullable=False, server_default="draft"),
    Column("published_at", DateTime(timezone=True), nullable=True),
    CheckConstraint("status IN ('draft', 'held', 'published')", name="ck_issues_status"),
    UniqueConstraint("week_of", name="uq_issues_week_of"),
)

# One row per item placed into an issue: a categorised, ordered story with a
# rule-based summary and a primary source URL. ``extra_source_urls`` is the
# ordered list of other outlets whose raw_items fell into the same cluster.
# Mirrors the ``items`` table created by migration 0003.
items_table = Table(
    "items",
    sources_metadata,
    Column("id", Integer, primary_key=True),
    Column(
        "issue_id",
        Integer,
        ForeignKey("issues.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    ),
    Column(
        "cluster_id",
        Integer,
        ForeignKey("clusters.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    ),
    Column("category", String(64), nullable=False, index=True),
    Column("position", Integer, nullable=False),
    Column("headline", String(1024), nullable=False),
    Column("summary", Text, nullable=False),
    Column("primary_url", String(2048), nullable=False),
    Column("extra_source_urls", JSON, nullable=False, server_default="[]"),
    UniqueConstraint("issue_id", "position", name="uq_items_issue_position"),
    UniqueConstraint("issue_id", "cluster_id", name="uq_items_issue_cluster"),
)


@dataclass(frozen=True)
class SourceSpec:
    """A single entry from ``sources.yaml`` after validation."""

    url: str
    kind: str
    category_hint: str
    name: str | None = None


class SourceRegistryError(ValueError):
    """Raised when ``sources.yaml`` is malformed or contains invalid values."""


def load_sources_yaml(path: str | Path | None = None) -> list[SourceSpec]:
    """Read and validate the source registry from disk.

    A missing ``sources:`` key, duplicate URLs, unknown ``kind``/
    ``category_hint`` values, or non-string URLs all raise
    :class:`SourceRegistryError` — the loader refuses to silently drop
    entries so a typo in the YAML fails loudly at boot instead of at
    publication time.
    """
    resolved = Path(path) if path is not None else DEFAULT_SOURCES_YAML
    try:
        raw_text = resolved.read_text(encoding="utf-8")
    except OSError as exc:
        raise SourceRegistryError(f"could not read {resolved}: {exc}") from exc

    data = yaml.safe_load(raw_text)
    return _parse_document(data, source=str(resolved))


def _parse_document(data: Any, *, source: str) -> list[SourceSpec]:
    if not isinstance(data, dict) or "sources" not in data:
        raise SourceRegistryError(f"{source}: expected a mapping with a 'sources' key")
    entries = data["sources"]
    if not isinstance(entries, list) or not entries:
        raise SourceRegistryError(f"{source}: 'sources' must be a non-empty list")

    specs: list[SourceSpec] = []
    seen_urls: set[str] = set()
    for index, entry in enumerate(entries):
        spec = _parse_entry(entry, source=source, index=index)
        if spec.url in seen_urls:
            raise SourceRegistryError(f"{source}: duplicate url {spec.url!r} at index {index}")
        seen_urls.add(spec.url)
        specs.append(spec)
    return specs


def _parse_entry(entry: Any, *, source: str, index: int) -> SourceSpec:
    where = f"{source}: entry #{index}"
    if not isinstance(entry, dict):
        raise SourceRegistryError(f"{where}: expected a mapping, got {type(entry).__name__}")

    url = entry.get("url")
    kind = entry.get("kind")
    category_hint = entry.get("category_hint")
    name = entry.get("name")

    if not isinstance(url, str) or not url.strip():
        raise SourceRegistryError(f"{where}: 'url' must be a non-empty string")
    if not isinstance(kind, str) or kind not in SOURCE_KINDS:
        raise SourceRegistryError(
            f"{where}: 'kind' must be one of {sorted(SOURCE_KINDS)}, got {kind!r}"
        )
    if not isinstance(category_hint, str) or category_hint not in CATEGORY_HINTS:
        raise SourceRegistryError(
            f"{where}: 'category_hint' must be one of {sorted(CATEGORY_HINTS)}, "
            f"got {category_hint!r}"
        )
    if name is not None and not isinstance(name, str):
        raise SourceRegistryError(f"{where}: 'name', if given, must be a string")

    return SourceSpec(
        url=url.strip(),
        kind=kind,
        category_hint=category_hint,
        name=name.strip() if isinstance(name, str) else None,
    )


@dataclass(frozen=True)
class UpsertResult:
    """Summary of an :func:`upsert_sources` run."""

    inserted: int
    updated: int
    unchanged: int

    @property
    def total(self) -> int:
        return self.inserted + self.updated + self.unchanged


def upsert_sources(
    bind: Session | Connection,
    specs: Iterable[SourceSpec],
) -> UpsertResult:
    """Insert or update rows in ``sources`` from ``specs``.

    An entry whose ``url`` already exists has its ``kind``, ``category_hint``
    and ``active`` flag brought back into line with the YAML — this is how a
    hint reclassification or a temporarily-disabled source flips back on.
    Rows already present but not mentioned in ``specs`` are left untouched;
    retiring a source is a separate, deliberate operation.
    """
    connection = _as_connection(bind)

    inserted = 0
    updated = 0
    unchanged = 0

    for spec in specs:
        existing = connection.execute(
            select(
                sources_table.c.id,
                sources_table.c.kind,
                sources_table.c.category_hint,
                sources_table.c.active,
            ).where(sources_table.c.url == spec.url)
        ).first()

        if existing is None:
            connection.execute(
                sources_table.insert().values(
                    url=spec.url,
                    kind=spec.kind,
                    category_hint=spec.category_hint,
                    active=True,
                )
            )
            inserted += 1
            continue

        needs_update = (
            existing.kind != spec.kind
            or existing.category_hint != spec.category_hint
            or bool(existing.active) is not True
        )
        if needs_update:
            connection.execute(
                sources_table.update()
                .where(sources_table.c.id == existing.id)
                .values(
                    kind=spec.kind,
                    category_hint=spec.category_hint,
                    active=True,
                )
            )
            updated += 1
        else:
            unchanged += 1

    return UpsertResult(inserted=inserted, updated=updated, unchanged=unchanged)


def upsert_sources_from_yaml(
    bind: Session | Connection,
    path: str | Path | None = None,
) -> UpsertResult:
    """Convenience: parse ``sources.yaml`` and upsert its contents."""
    return upsert_sources(bind, load_sources_yaml(path))


def _as_connection(bind: Session | Connection) -> Connection:
    if isinstance(bind, Session):
        return bind.connection()
    return bind
