"""item byline: source name + source publish date

Revision ID: 0008_item_byline
Revises: 0007_source_category_lock
Create Date: 2026-09-25 00:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_item_byline"
down_revision: str | None = "0007_source_category_lock"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Publication name from sources.yaml; synced at boot.
    with op.batch_alter_table("sources") as batch:
        batch.add_column(sa.Column("name", sa.String(length=255), nullable=True))
    # The feed entry's own published/updated stamp.
    with op.batch_alter_table("raw_items") as batch:
        batch.add_column(sa.Column("published_at", sa.DateTime(timezone=True), nullable=True))
    # Per-item byline, frozen at build time like headline/summary.
    with op.batch_alter_table("items") as batch:
        batch.add_column(sa.Column("source_name", sa.String(length=255), nullable=True))
        batch.add_column(
            sa.Column("source_published_at", sa.DateTime(timezone=True), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("items") as batch:
        batch.drop_column("source_published_at")
        batch.drop_column("source_name")
    with op.batch_alter_table("raw_items") as batch:
        batch.drop_column("published_at")
    with op.batch_alter_table("sources") as batch:
        batch.drop_column("name")
