"""Alembic environment for the Signalweek data layer."""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from signalweek.db.session import DEFAULT_DATABASE_URL
from signalweek.sources import sources_metadata

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Precedence: URL already on the Config (caller/test override) wins,
# then DATABASE_URL env var, then the sqlite default.
existing_url = config.get_main_option("sqlalchemy.url")
resolved_url = existing_url or os.environ.get("DATABASE_URL") or DEFAULT_DATABASE_URL
config.set_main_option("sqlalchemy.url", resolved_url)

# The curated-digest schema is defined as SQLAlchemy Core tables — no
# declarative ORM base. Handing this metadata to Alembic lets ``--autogenerate``
# still diff against the shipped Core definitions.
target_metadata = sources_metadata


def run_migrations_offline() -> None:
    """Run migrations without a live DB connection."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations with a live connection, using batch mode on SQLite."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        is_sqlite = connection.dialect.name == "sqlite"
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=is_sqlite,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
