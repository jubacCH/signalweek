"""Verify the Alembic baseline migration upgrades cleanly."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

REPO_ROOT = Path(__file__).resolve().parent.parent
ALEMBIC_INI = REPO_ROOT / "alembic.ini"
MIGRATIONS_DIR = REPO_ROOT / "src" / "signalweek" / "db" / "migrations"


def _make_config(url: str) -> Config:
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    config.cmd_opts = type("Opts", (), {"x": [f"url={url}"]})()
    return config


def test_upgrade_head_creates_expected_tables(tmp_path: Path) -> None:
    db_file = tmp_path / "signalweek.db"
    sync_url = f"sqlite:///{db_file}"
    async_url = f"sqlite+aiosqlite:///{db_file}"

    config = _make_config(async_url)
    command.upgrade(config, "head")

    inspector = inspect(create_engine(sync_url))
    tables = set(inspector.get_table_names())

    assert {"issues", "signal_items", "subscribers"} <= tables


def test_downgrade_removes_tables(tmp_path: Path) -> None:
    db_file = tmp_path / "signalweek.db"
    sync_url = f"sqlite:///{db_file}"
    async_url = f"sqlite+aiosqlite:///{db_file}"

    config = _make_config(async_url)
    command.upgrade(config, "head")
    command.downgrade(config, "base")

    inspector = inspect(create_engine(sync_url))
    tables = set(inspector.get_table_names())

    for name in ("issues", "signal_items", "subscribers"):
        assert name not in tables, f"expected {name!r} dropped after downgrade"


def test_baseline_revision_is_0001_init() -> None:
    from alembic.script import ScriptDirectory

    config = _make_config("sqlite+aiosqlite:///:memory:")
    script = ScriptDirectory.from_config(config)
    heads = script.get_heads()
    assert list(heads) == ["0001_init"], heads
