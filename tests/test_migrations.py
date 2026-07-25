"""Verify the initial Alembic migration produces the expected schema."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

REPO_ROOT = Path(__file__).resolve().parents[1]


def _alembic_config(db_url: str) -> Config:
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


def test_upgrade_head_creates_all_tables(tmp_path: Path) -> None:
    db_path = tmp_path / "migrated.db"
    db_url = f"sqlite:///{db_path}"
    command.upgrade(_alembic_config(db_url), "head")

    engine = create_engine(db_url)
    try:
        tables = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()

    expected = {"users", "sources", "signals", "digests", "api_tokens", "alembic_version"}
    assert expected.issubset(tables)


def test_downgrade_removes_all_tables(tmp_path: Path) -> None:
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

    for name in ("users", "sources", "signals", "digests", "api_tokens"):
        assert name not in tables
