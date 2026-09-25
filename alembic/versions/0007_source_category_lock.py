"""sources.category_locked: pin single-category sources to their hint

Revision ID: 0007_source_category_lock
Revises: 0006_alerts_pipeline_runs
Create Date: 2026-09-25 00:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_source_category_lock"
down_revision: str | None = "0006_alerts_pipeline_runs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # A locked source's category_hint beats keyword matches in the classifier.
    with op.batch_alter_table("sources") as batch:
        batch.add_column(
            sa.Column(
                "category_locked",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
    # arXiv feeds only ever carry research papers.
    op.execute(
        sa.text("UPDATE sources SET category_locked = :locked WHERE kind = 'arxiv_rss'").bindparams(
            locked=True
        )
    )


def downgrade() -> None:
    with op.batch_alter_table("sources") as batch:
        batch.drop_column("category_locked")
