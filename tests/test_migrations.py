"""Verify the Alembic migration chain produces the expected schema."""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

REPO_ROOT = Path(__file__).resolve().parents[1]

CURATED_TABLES = {"sources", "raw_items", "clusters", "issues", "items"}
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

    assert columns["sources"] == {"id", "url", "kind", "category_hint", "active"}
    assert columns["raw_items"] == {
        "id",
        "source_id",
        "url",
        "canonical_url",
        "title",
        "body",
        "fetched_at",
        "first_seen_at",
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


def test_downgrade_one_restores_personal_aggregator_schema(tmp_path: Path) -> None:
    db_path = tmp_path / "migrated.db"
    db_url = f"sqlite:///{db_path}"
    cfg = _alembic_config(db_url)

    command.upgrade(cfg, "head")
    command.downgrade(cfg, "-1")

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
