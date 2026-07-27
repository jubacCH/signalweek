"""source health: failure counters, silence markers, deactivation audit log

Revision ID: 0005_source_health
Revises: 0004_source_discovery
Create Date: 2026-07-27 00:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_source_health"
down_revision: str | None = "0004_source_discovery"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Health counters kept on the source itself so the prune step can decide
    # deactivation/reactivation from a single row read. ``last_item_at`` is the
    # newest first_seen_at across the source's raw_items; the ingest layer
    # bumps it whenever an insert actually lands.
    with op.batch_alter_table("sources") as batch:
        batch.add_column(
            sa.Column(
                "consecutive_fetch_failures",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )
        batch.add_column(sa.Column("last_fetch_ok_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(
            sa.Column("last_fetch_error_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch.add_column(sa.Column("last_item_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("deactivated_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("deactivation_reason", sa.String(length=64), nullable=True))

    # Append-only audit log: every activation/deactivation the prune step
    # decides is recorded here with a machine-readable ``reason`` so operators
    # can see the sequence of state changes after the fact.
    op.create_table(
        "source_health_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"], ondelete="CASCADE"),
        sa.CheckConstraint(
            "action IN ('activated', 'deactivated')",
            name="ck_source_health_events_action",
        ),
    )
    op.create_index(
        "ix_source_health_events_source_id",
        "source_health_events",
        ["source_id"],
    )
    op.create_index("ix_source_health_events_at", "source_health_events", ["at"])


def downgrade() -> None:
    op.drop_index("ix_source_health_events_at", table_name="source_health_events")
    op.drop_index("ix_source_health_events_source_id", table_name="source_health_events")
    op.drop_table("source_health_events")

    with op.batch_alter_table("sources") as batch:
        batch.drop_column("deactivation_reason")
        batch.drop_column("deactivated_at")
        batch.drop_column("last_item_at")
        batch.drop_column("last_fetch_error_at")
        batch.drop_column("last_fetch_ok_at")
        batch.drop_column("consecutive_fetch_failures")
