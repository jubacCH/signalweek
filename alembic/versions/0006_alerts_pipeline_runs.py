"""pipeline reliability: alerts + pipeline_runs

Revision ID: 0006_alerts_pipeline_runs
Revises: 0005_source_health
Create Date: 2026-09-25 00:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_alerts_pipeline_runs"
down_revision: str | None = "0005_source_health"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Append-only record of anything an operator must know about: a held
    # week, a weekly slot that passed without an issue, or a job that raised.
    op.create_table(
        "alerts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason", sa.String(length=32), nullable=False),
        sa.Column("job", sa.String(length=32), nullable=True),
        sa.Column("week_of", sa.Date(), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "reason IN ('insufficient_items', 'missed_run', 'pipeline_failed')",
            name="ck_alerts_reason",
        ),
    )
    op.create_index("ix_alerts_created_at", "alerts", ["created_at"])
    op.create_index("ix_alerts_reason", "alerts", ["reason"])

    # One row per scheduled job execution, with wall-clock timing.
    op.create_table(
        "pipeline_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("job", sa.String(length=32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("item_count", sa.Integer(), nullable=True),
        sa.Column("week_of", sa.Date(), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status IN ('running', 'ok', 'failed', 'skipped')",
            name="ck_pipeline_runs_status",
        ),
    )
    op.create_index("ix_pipeline_runs_job", "pipeline_runs", ["job"])
    op.create_index("ix_pipeline_runs_started_at", "pipeline_runs", ["started_at"])


def downgrade() -> None:
    op.drop_index("ix_pipeline_runs_started_at", table_name="pipeline_runs")
    op.drop_index("ix_pipeline_runs_job", table_name="pipeline_runs")
    op.drop_table("pipeline_runs")
    op.drop_index("ix_alerts_reason", table_name="alerts")
    op.drop_index("ix_alerts_created_at", table_name="alerts")
    op.drop_table("alerts")
