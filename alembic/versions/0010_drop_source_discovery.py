"""drop source discovery: source_candidates table + discovered* on sources

Revision ID: 0010_drop_source_discovery
Revises: 0009_cluster_embeddings
Create Date: 2026-09-25 00:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_drop_source_discovery"
down_revision: str | None = "0009_cluster_embeddings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Reverses 0004: the auto-discovery registry was never approved (AIC-30)
    # and its code is gone; every source is curated via sources.yaml / CLI.
    op.drop_index("ix_source_candidates_promoted", table_name="source_candidates")
    op.drop_index("ix_source_candidates_domain", table_name="source_candidates")
    op.drop_table("source_candidates")

    with op.batch_alter_table("sources") as batch:
        batch.drop_column("discovered_cite_count")
        batch.drop_column("discovered_first_seen_week")
        batch.drop_column("discovered")


def downgrade() -> None:
    with op.batch_alter_table("sources") as batch:
        batch.add_column(
            sa.Column(
                "discovered",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch.add_column(sa.Column("discovered_first_seen_week", sa.Date(), nullable=True))
        batch.add_column(sa.Column("discovered_cite_count", sa.Integer(), nullable=True))

    op.create_table(
        "source_candidates",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("domain", sa.String(length=255), nullable=False),
        sa.Column("first_seen_week", sa.Date(), nullable=False),
        sa.Column("last_seen_week", sa.Date(), nullable=False),
        sa.Column("cite_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "distinct_weeks_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "promoted",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("promoted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("promoted_source_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ["promoted_source_id"],
            ["sources.id"],
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint("domain", name="uq_source_candidates_domain"),
    )
    op.create_index("ix_source_candidates_domain", "source_candidates", ["domain"])
    op.create_index("ix_source_candidates_promoted", "source_candidates", ["promoted"])
