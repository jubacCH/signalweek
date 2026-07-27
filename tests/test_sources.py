"""Tests for the static source registry and its YAML loader."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.engine import Engine

from signalweek.db.session import create_db_engine
from signalweek.sources import (
    CATEGORY_HINTS,
    DEFAULT_SOURCES_YAML,
    SOURCE_KINDS,
    SourceRegistryError,
    SourceSpec,
    load_sources_yaml,
    sources_metadata,
    sources_table,
    upsert_sources,
    upsert_sources_from_yaml,
)

REQUIRED_CATEGORIES = {
    "models",
    "lawsuits_policy",
    "funding",
    "research",
    "industry_moves",
}


@pytest.fixture()
def sources_engine() -> Iterator[Engine]:
    """Fresh in-memory SQLite engine with only the ``sources`` table created."""
    engine = create_db_engine("sqlite:///:memory:")
    sources_metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# The checked-in sources.yaml itself
# ---------------------------------------------------------------------------


class TestCheckedInRegistry:
    def test_yaml_file_exists_at_repo_root(self) -> None:
        assert DEFAULT_SOURCES_YAML.is_file(), f"sources.yaml missing at {DEFAULT_SOURCES_YAML}"

    def test_loads_at_least_20_sources(self) -> None:
        specs = load_sources_yaml()
        assert len(specs) >= 20, f"expected >= 20 seeded sources, got {len(specs)}"

    def test_every_source_has_valid_kind_and_category_hint(self) -> None:
        for spec in load_sources_yaml():
            assert spec.kind in SOURCE_KINDS, f"{spec.url}: bad kind {spec.kind!r}"
            assert spec.category_hint in CATEGORY_HINTS, (
                f"{spec.url}: bad category_hint {spec.category_hint!r}"
            )

    def test_covers_every_required_category(self) -> None:
        hints = {spec.category_hint for spec in load_sources_yaml()}
        missing = REQUIRED_CATEGORIES - hints
        assert not missing, f"registry is missing categories: {sorted(missing)}"

    def test_covers_expected_frontier_labs(self) -> None:
        urls = {spec.url for spec in load_sources_yaml()}
        for needle in (
            "openai.com",
            "anthropic.com",
            "deepmind.google",
            "ai.meta.com",
            "mistral.ai",
            "x.ai",
        ):
            assert any(needle in u for u in urls), f"no source URL contains {needle!r}"

    def test_includes_arxiv_cs_ai_and_cs_lg(self) -> None:
        urls = {spec.url for spec in load_sources_yaml()}
        assert any("arxiv.org/rss/cs.AI" in u for u in urls)
        assert any("arxiv.org/rss/cs.LG" in u for u in urls)

    def test_includes_expected_policy_regulators(self) -> None:
        urls = {spec.url for spec in load_sources_yaml()}
        assert any("ftc.gov" in u for u in urls), "FTC feed missing"
        assert any("ec.europa.eu" in u for u in urls), "EU digital-strategy feed missing"
        assert any("whitehouse.gov" in u for u in urls), "White House OSTP feed missing"

    def test_urls_are_unique(self) -> None:
        urls = [spec.url for spec in load_sources_yaml()]
        assert len(urls) == len(set(urls)), "duplicate URLs in sources.yaml"


# ---------------------------------------------------------------------------
# YAML parsing / validation
# ---------------------------------------------------------------------------


class TestLoaderValidation:
    def _write(self, tmp_path: Path, body: str) -> Path:
        path = tmp_path / "sources.yaml"
        path.write_text(body, encoding="utf-8")
        return path

    def test_rejects_missing_sources_key(self, tmp_path: Path) -> None:
        p = self._write(tmp_path, "other: []\n")
        with pytest.raises(SourceRegistryError, match="'sources' key"):
            load_sources_yaml(p)

    def test_rejects_empty_sources_list(self, tmp_path: Path) -> None:
        p = self._write(tmp_path, "sources: []\n")
        with pytest.raises(SourceRegistryError, match="non-empty list"):
            load_sources_yaml(p)

    def test_rejects_unknown_kind(self, tmp_path: Path) -> None:
        p = self._write(
            tmp_path,
            "sources:\n"
            "  - url: https://example.com/feed\n"
            "    kind: telepathy\n"
            "    category_hint: models\n",
        )
        with pytest.raises(SourceRegistryError, match="'kind' must be one of"):
            load_sources_yaml(p)

    def test_rejects_unknown_category_hint(self, tmp_path: Path) -> None:
        p = self._write(
            tmp_path,
            "sources:\n"
            "  - url: https://example.com/feed\n"
            "    kind: rss\n"
            "    category_hint: gossip\n",
        )
        with pytest.raises(SourceRegistryError, match="'category_hint' must be one of"):
            load_sources_yaml(p)

    def test_rejects_missing_url(self, tmp_path: Path) -> None:
        p = self._write(
            tmp_path,
            "sources:\n  - kind: rss\n    category_hint: models\n",
        )
        with pytest.raises(SourceRegistryError, match="'url' must be a non-empty string"):
            load_sources_yaml(p)

    def test_rejects_duplicate_urls(self, tmp_path: Path) -> None:
        p = self._write(
            tmp_path,
            "sources:\n"
            "  - url: https://example.com/feed\n"
            "    kind: rss\n"
            "    category_hint: models\n"
            "  - url: https://example.com/feed\n"
            "    kind: atom\n"
            "    category_hint: research\n",
        )
        with pytest.raises(SourceRegistryError, match="duplicate url"):
            load_sources_yaml(p)

    def test_missing_file_raises_registry_error(self, tmp_path: Path) -> None:
        with pytest.raises(SourceRegistryError, match="could not read"):
            load_sources_yaml(tmp_path / "no_such_file.yaml")

    def test_strips_whitespace_from_url_and_name(self, tmp_path: Path) -> None:
        p = self._write(
            tmp_path,
            "sources:\n"
            "  - name: '  Example  '\n"
            "    url: '  https://example.com/feed  '\n"
            "    kind: rss\n"
            "    category_hint: models\n",
        )
        [spec] = load_sources_yaml(p)
        assert spec.url == "https://example.com/feed"
        assert spec.name == "Example"


# ---------------------------------------------------------------------------
# Upsert semantics
# ---------------------------------------------------------------------------


class TestUpsert:
    def test_inserts_all_new_rows(self, sources_engine: Engine) -> None:
        specs = [
            SourceSpec(url="https://a.example/feed", kind="rss", category_hint="models"),
            SourceSpec(url="https://b.example/feed", kind="atom", category_hint="research"),
        ]
        with sources_engine.begin() as conn:
            result = upsert_sources(conn, specs)
            rows = list(conn.execute(select(sources_table).order_by(sources_table.c.url)))

        assert (result.inserted, result.updated, result.unchanged) == (2, 0, 0)
        assert [(r.url, r.kind, r.category_hint, bool(r.active)) for r in rows] == [
            ("https://a.example/feed", "rss", "models", True),
            ("https://b.example/feed", "atom", "research", True),
        ]

    def test_is_idempotent_when_rerun_unchanged(self, sources_engine: Engine) -> None:
        specs = [
            SourceSpec(url="https://a.example/feed", kind="rss", category_hint="models"),
        ]
        with sources_engine.begin() as conn:
            upsert_sources(conn, specs)
        with sources_engine.begin() as conn:
            second = upsert_sources(conn, specs)
            row_count = conn.execute(select(sources_table)).all()

        assert (second.inserted, second.updated, second.unchanged) == (0, 0, 1)
        assert len(row_count) == 1

    def test_updates_kind_and_category_hint_on_existing_row(self, sources_engine: Engine) -> None:
        original = SourceSpec(url="https://a.example/feed", kind="rss", category_hint="models")
        revised = SourceSpec(url="https://a.example/feed", kind="atom", category_hint="research")

        with sources_engine.begin() as conn:
            upsert_sources(conn, [original])
        with sources_engine.begin() as conn:
            result = upsert_sources(conn, [revised])
            row = conn.execute(select(sources_table)).one()

        assert (result.inserted, result.updated, result.unchanged) == (0, 1, 0)
        assert row.kind == "atom"
        assert row.category_hint == "research"

    def test_reactivates_a_previously_deactivated_source(self, sources_engine: Engine) -> None:
        spec = SourceSpec(url="https://a.example/feed", kind="rss", category_hint="models")
        with sources_engine.begin() as conn:
            upsert_sources(conn, [spec])
            conn.execute(
                sources_table.update().where(sources_table.c.url == spec.url).values(active=False)
            )
        with sources_engine.begin() as conn:
            result = upsert_sources(conn, [spec])
            row = conn.execute(select(sources_table)).one()

        assert (result.inserted, result.updated, result.unchanged) == (0, 1, 0)
        assert bool(row.active) is True

    def test_does_not_touch_rows_absent_from_spec_list(self, sources_engine: Engine) -> None:
        with sources_engine.begin() as conn:
            upsert_sources(
                conn,
                [
                    SourceSpec(url="https://a.example/feed", kind="rss", category_hint="models"),
                    SourceSpec(
                        url="https://b.example/feed",
                        kind="rss",
                        category_hint="research",
                    ),
                ],
            )
        with sources_engine.begin() as conn:
            upsert_sources(
                conn,
                [SourceSpec(url="https://a.example/feed", kind="rss", category_hint="models")],
            )
            urls = sorted(r.url for r in conn.execute(select(sources_table)))

        assert urls == ["https://a.example/feed", "https://b.example/feed"]

    def test_upsert_from_yaml_seeds_the_full_registry(self, sources_engine: Engine) -> None:
        with sources_engine.begin() as conn:
            result = upsert_sources_from_yaml(conn)
            rows = conn.execute(select(sources_table)).all()

        expected = len(load_sources_yaml())
        assert result.inserted == expected
        assert result.updated == 0
        assert len(rows) == expected
