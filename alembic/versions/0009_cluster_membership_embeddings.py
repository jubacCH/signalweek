"""cluster membership + headline embeddings on raw_items

Revision ID: 0009_cluster_embeddings
Revises: 0008_item_byline
Create Date: 2026-09-25 00:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_cluster_embeddings"
down_revision: str | None = "0008_item_byline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Both start NULL: the next ingest tick clusters (and embeds) every
    # existing raw_item once, then only new ones.
    with op.batch_alter_table("raw_items") as batch:
        batch.add_column(sa.Column("cluster_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("title_embedding", sa.LargeBinary(), nullable=True))
        batch.create_foreign_key(
            "fk_raw_items_cluster_id",
            "clusters",
            ["cluster_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch.create_index("ix_raw_items_cluster_id", ["cluster_id"])


def downgrade() -> None:
    with op.batch_alter_table("raw_items") as batch:
        batch.drop_index("ix_raw_items_cluster_id")
        batch.drop_constraint("fk_raw_items_cluster_id", type_="foreignkey")
        batch.drop_column("title_embedding")
        batch.drop_column("cluster_id")
