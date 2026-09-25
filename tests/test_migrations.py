"""Verify the Alembic migration chain produces the expected schema."""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

REPO_ROOT = Path(__file__).resolve().parents[1]

CURATED_TABLES = {
    "sources",
    "raw_items",
    "clusters",
    "issues",
    "items",
    "source_candidates",
    "source_health_events",
    "alerts",
    "pipeline_runs",
}
PERSONAL_AGGREGATOR_TABLES = {"users", "signals", "digests", "api_tokens"}


def _alembic_config(db_url: str) -> Config:
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


def test_upgrade_head_creates_curated_digest_tables(tmp_path: Path) -> None:
    db_path = tmp_path / "migrated.db"
    db_url = f"sqlite:///{db_path}"
    command.upgrade(_alembic_config(db_url), "head")

    engine = create_engine(db_url)
    try:
        tables = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()

    assert CURATED_TABLES.issubset(tables)
    assert "alembic_version" in tables


def test_upgrade_head_drops_personal_aggregator_tables(tmp_path: Path) -> None:
    db_path = tmp_path / "migrated.db"
    db_url = f"sqlite:///{db_path}"
    command.upgrade(_alembic_config(db_url), "head")

    engine = create_engine(db_url)
    try:
        tables = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()

    assert PERSONAL_AGGREGATOR_TABLES.isdisjoint(tables)


def test_curated_tables_have_expected_columns(tmp_path: Path) -> None:
    db_path = tmp_path / "migrated.db"
    db_url = f"sqlite:///{db_path}"
    command.upgrade(_alembic_config(db_url), "head")

    engine = create_engine(db_url)
    try:
        inspector = inspect(engine)
        columns = {
            table: {col["name"] for col in inspector.get_columns(table)} for table in CURATED_TABLES
        }
    finally:
        engine.dispose()

    assert columns["sources"] == {
        "id",
        "url",
        "kind",
        "category_hint",
        "name",
        "category_locked",
        "active",
        "discovered",
        "discovered_first_seen_week",
        "discovered_cite_count",
        "consecutive_fetch_failures",
        "last_fetch_ok_at",
        "last_fetch_error_at",
        "last_item_at",
        "deactivated_at",
        "deactivation_reason",
    }
    assert columns["source_candidates"] == {
        "id",
        "domain",
        "first_seen_week",
        "last_seen_week",
        "cite_count",
        "distinct_weeks_count",
        "promoted",
        "promoted_at",
        "promoted_source_id",
    }
    assert columns["source_health_events"] == {
        "id",
        "source_id",
        "at",
        "action",
        "reason",
    }
    assert columns["raw_items"] == {
        "id",
        "source_id",
        "url",
        "canonical_url",
        "title",
        "body",
        "fetched_at",
        "first_seen_at",
        "published_at",
        "cluster_id",
        "title_embedding",
    }
    assert columns["clusters"] == {
        "id",
        "primary_url",
        "category",
        "canonical_headline",
    }
    assert columns["issues"] == {"id", "week_of", "status", "published_at"}
    assert columns["items"] == {
        "id",
        "issue_id",
        "cluster_id",
        "category",
        "position",
        "headline",
        "summary",
        "primary_url",
        "extra_source_urls",
        "source_name",
        "source_published_at",
    }


def test_sources_table_has_no_user_id_column(tmp_path: Path) -> None:
    db_path = tmp_path / "migrated.db"
    db_url = f"sqlite:///{db_path}"
    command.upgrade(_alembic_config(db_url), "head")

    engine = create_engine(db_url)
    try:
        source_columns = {c["name"] for c in inspect(engine).get_columns("sources")}
    finally:
        engine.dispose()

    assert "user_id" not in source_columns


def test_issues_week_of_is_unique(tmp_path: Path) -> None:
    db_path = tmp_path / "migrated.db"
    db_url = f"sqlite:///{db_path}"
    command.upgrade(_alembic_config(db_url), "head")

    engine = create_engine(db_url)
    try:
        with engine.begin() as conn:
            conn.execute(
                text("INSERT INTO issues (week_of, status) VALUES ('2026-07-20', 'draft')")
            )
            with pytest.raises(IntegrityError):
                conn.execute(
                    text("INSERT INTO issues (week_of, status) VALUES ('2026-07-20', 'draft')")
                )
    finally:
        engine.dispose()


def test_issues_status_check_constraint_rejects_bad_values(tmp_path: Path) -> None:
    db_path = tmp_path / "migrated.db"
    db_url = f"sqlite:///{db_path}"
    command.upgrade(_alembic_config(db_url), "head")

    engine = create_engine(db_url)
    try:
        with engine.begin() as conn:
            with pytest.raises(IntegrityError):
                conn.execute(
                    text(
                        "INSERT INTO issues (week_of, status) VALUES ('2026-07-20', 'not-a-status')"
                    )
                )
    finally:
        engine.dispose()


def test_issues_status_check_constraint_accepts_all_valid_values(tmp_path: Path) -> None:
    db_path = tmp_path / "migrated.db"
    db_url = f"sqlite:///{db_path}"
    command.upgrade(_alembic_config(db_url), "head")

    engine = create_engine(db_url)
    try:
        with engine.begin() as conn:
            for i, status in enumerate(("draft", "held", "published")):
                conn.execute(
                    text("INSERT INTO issues (week_of, status) VALUES (:w, :s)"),
                    {"w": f"2026-07-{20 + i:02d}", "s": status},
                )
    finally:
        engine.dispose()


def test_downgrade_restores_personal_aggregator_schema(tmp_path: Path) -> None:
    db_path = tmp_path / "migrated.db"
    db_url = f"sqlite:///{db_path}"
    cfg = _alembic_config(db_url)

    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0002_api_tokens")

    engine = create_engine(db_url)
    try:
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        source_columns = {c["name"] for c in inspector.get_columns("sources")}
    finally:
        engine.dispose()

    assert PERSONAL_AGGREGATOR_TABLES.issubset(tables)
    # `sources` exists in both schemas — after downgrade it should be the
    # per-user variant, not the curated-digest one.
    assert "user_id" in source_columns
    # The curated-only tables must be gone.
    assert (CURATED_TABLES - {"sources"}).isdisjoint(tables)


def test_downgrade_one_removes_source_health_additions(tmp_path: Path) -> None:
    db_path = tmp_path / "migrated.db"
    db_url = f"sqlite:///{db_path}"
    cfg = _alembic_config(db_url)

    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0004_source_discovery")

    engine = create_engine(db_url)
    try:
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        source_columns = {c["name"] for c in inspector.get_columns("sources")}
    finally:
        engine.dispose()

    # After downgrading to 0004, we are back on the source-discovery
    # schema — the health-tracking additions are gone.
    assert "source_health_events" not in tables
    assert "consecutive_fetch_failures" not in source_columns
    assert "last_fetch_ok_at" not in source_columns
    assert "last_fetch_error_at" not in source_columns
    assert "last_item_at" not in source_columns
    assert "deactivated_at" not in source_columns
    assert "deactivation_reason" not in source_columns
    # Source discovery columns are still there.
    assert "discovered" in source_columns
    assert "source_candidates" in tables
    # And the other curated tables.
    assert (CURATED_TABLES - {"source_health_events", "alerts", "pipeline_runs"}).issubset(tables)


def test_downgrade_below_0006_removes_alerts_and_pipeline_runs(tmp_path: Path) -> None:
    db_url = f"sqlite:///{tmp_path / 'migrated.db'}"
    cfg = _alembic_config(db_url)

    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0005_source_health")

    engine = create_engine(db_url)
    try:
        tables = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()

    assert "alerts" not in tables
    assert "pipeline_runs" not in tables
    assert "source_health_events" in tables


def test_alerts_reason_check_constraint(tmp_path: Path) -> None:
    db_url = f"sqlite:///{tmp_path / 'migrated.db'}"
    command.upgrade(_alembic_config(db_url), "head")

    engine = create_engine(db_url)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO alerts (created_at, reason) "
                    "VALUES ('2026-09-25 00:00:00', 'missed_run')"
                )
            )
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO alerts (created_at, reason) "
                    "VALUES ('2026-09-25 00:00:00', 'bogus')"
                )
            )
    finally:
        engine.dispose()


def test_downgrade_to_base_removes_all_tables(tmp_path: Path) -> None:
    db_path = tmp_path / "migrated.db"
    db_url = f"sqlite:///{db_path}"
    cfg = _alembic_config(db_url)

    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")

    engine = create_engine(db_url)
    try:
        tables = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()

    for name in CURATED_TABLES | PERSONAL_AGGREGATOR_TABLES:
        assert name not in tables


def test_upgrade_after_downgrade_reapplies_cleanly(tmp_path: Path) -> None:
    db_path = tmp_path / "migrated.db"
    db_url = f"sqlite:///{db_path}"
    cfg = _alembic_config(db_url)

    command.upgrade(cfg, "head")
    command.downgrade(cfg, "-1")
    command.upgrade(cfg, "head")

    engine = create_engine(db_url)
    try:
        tables = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()

    assert CURATED_TABLES.issubset(tables)
    assert PERSONAL_AGGREGATOR_TABLES.isdisjoint(tables)


def test_0007_locks_existing_arxiv_sources(tmp_path: Path) -> None:
    db_url = f"sqlite:///{tmp_path / 'lock.db'}"
    cfg = _alembic_config(db_url)
    command.upgrade(cfg, "0006_alerts_pipeline_runs")
    engine = create_engine(db_url)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO sources (url, kind, category_hint) VALUES "
                "('https://arxiv.org/rss/cs.AI', 'arxiv_rss', 'research'), "
                "('https://press.example/feed', 'rss', 'funding')"
            )
        )
    command.upgrade(cfg, "head")
    with engine.connect() as conn:
        rows = dict(conn.execute(text("SELECT url, category_locked FROM sources")).all())
    engine.dispose()
    assert rows == {"https://arxiv.org/rss/cs.AI": 1, "https://press.example/feed": 0}


def test_downgrade_one_drops_category_locked(tmp_path: Path) -> None:
    db_url = f"sqlite:///{tmp_path / 'migrated.db'}"
    cfg = _alembic_config(db_url)
    command.upgrade(cfg, "0007_source_category_lock")
    command.downgrade(cfg, "-1")
    engine = create_engine(db_url)
    try:
        columns = {c["name"] for c in inspect(engine).get_columns("sources")}
    finally:
        engine.dispose()
    assert "category_locked" not in columns


def test_downgrade_one_drops_cluster_membership_columns(tmp_path: Path) -> None:
    db_url = f"sqlite:///{tmp_path / 'migrated.db'}"
    cfg = _alembic_config(db_url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "-1")
    engine = create_engine(db_url)
    try:
        raw_items = {c["name"] for c in inspect(engine).get_columns("raw_items")}
    finally:
        engine.dispose()
    assert not {"cluster_id", "title_embedding"} & raw_items
    assert "published_at" in raw_items


def test_downgrade_drops_item_byline_columns(tmp_path: Path) -> None:
    db_url = f"sqlite:///{tmp_path / 'migrated.db'}"
    cfg = _alembic_config(db_url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0007_source_category_lock")
    engine = create_engine(db_url)
    try:
        inspector = inspect(engine)
        sources = {c["name"] for c in inspector.get_columns("sources")}
        raw_items = {c["name"] for c in inspector.get_columns("raw_items")}
        items = {c["name"] for c in inspector.get_columns("items")}
    finally:
        engine.dispose()
    assert "name" not in sources
    assert "published_at" not in raw_items
    assert not {"source_name", "source_published_at"} & items


def test_rebuilding_sources_does_not_cascade_delete_child_rows(tmp_path: Path) -> None:
    """SQLite batch mode recreates ``sources`` (copy, DROP, rename). With FKs on,
    the DROP cascaded into raw_items and wiped them in production (AIC-12)."""
    db_url = f"sqlite:///{tmp_path / 'fk.db'}"
    cfg = _alembic_config(db_url)
    command.upgrade(cfg, "0006_alerts_pipeline_runs")
    engine = create_engine(db_url)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO sources (id, url, kind, category_hint) "
                "VALUES (1, 'https://a.example/feed', 'rss', 'models')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO raw_items "
                "(source_id, url, canonical_url, title, fetched_at, first_seen_at) "
                "VALUES (1, 'https://a.example/1', 'https://a.example/1', 'T', "
                "'2026-09-25', '2026-09-25')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO source_health_events (source_id, at, action, reason) "
                "VALUES (1, '2026-09-25', 'deactivated', 'fetch_failures')"
            )
        )
    command.upgrade(cfg, "head")
    with engine.connect() as conn:
        raw_items = conn.execute(text("SELECT count(*) FROM raw_items")).scalar_one()
        events = conn.execute(text("SELECT count(*) FROM source_health_events")).scalar_one()
    engine.dispose()
    assert (raw_items, events) == (1, 1)
