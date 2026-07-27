"""source discovery: candidates table + discovered provenance on sources

Revision ID: 0004_source_discovery
Revises: 0003_curated_digest_schema
Create Date: 2026-07-27 00:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_source_discovery"
down_revision: str | None = "0003_curated_digest_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ``discovered`` flags a source that was auto-promoted from the citation
    # stream; the two provenance columns record when the domain first showed
    # up in a published/held item and how many item-level citations tipped it
    # over the promotion threshold.
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

    # Running tally of every domain that has been cited by an item, keyed by
    # domain. Mining rebuilds cite_count / distinct_weeks_count from the
    # current ``items`` table; promotion flips ``promoted`` and links back to
    # the row inserted in ``sources``.
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


def downgrade() -> None:
    op.drop_index("ix_source_candidates_promoted", table_name="source_candidates")
    op.drop_index("ix_source_candidates_domain", table_name="source_candidates")
    op.drop_table("source_candidates")

    with op.batch_alter_table("sources") as batch:
        batch.drop_column("discovered_cite_count")
        batch.drop_column("discovered_first_seen_week")
        batch.drop_column("discovered")
