"""Baseline schema: issues, signal_items, subscribers.

Revision ID: 0001_init
Revises:
Create Date: 2026-07-24

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0001_init"
down_revision: str | None = None
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_table(
        "issues",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("number", name="uq_issues_number"),
    )
    op.create_index("ix_issues_number", "issues", ["number"], unique=True)

    op.create_table(
        "signal_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("url", sa.String(length=2000), nullable=False),
        sa.Column("source", sa.String(length=200), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("issue_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ["issue_id"],
            ["issues.id"],
            name="fk_signal_items_issue_id_issues",
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint("url", name="uq_signal_items_url"),
    )
    op.create_index(
        "ix_signal_items_issue_id", "signal_items", ["issue_id"], unique=False
    )

    op.create_table(
        "subscribers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("unsubscribed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("email", name="uq_subscribers_email"),
    )
    op.create_index("ix_subscribers_email", "subscribers", ["email"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_subscribers_email", table_name="subscribers")
    op.drop_table("subscribers")

    op.drop_index("ix_signal_items_issue_id", table_name="signal_items")
    op.drop_table("signal_items")

    op.drop_index("ix_issues_number", table_name="issues")
    op.drop_table("issues")
