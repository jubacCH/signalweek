"""curated-digest schema: drop per-user tables, install issue/item pipeline

Revision ID: 0003_curated_digest_schema
Revises: 0002_api_tokens
Create Date: 2026-07-27 00:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_curated_digest_schema"
down_revision: str | None = "0002_api_tokens"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Drop the personal-aggregator tables in FK-safe order.
    op.drop_index("ix_api_tokens_token_hash", table_name="api_tokens")
    op.drop_index("ix_api_tokens_user_id", table_name="api_tokens")
    op.drop_table("api_tokens")

    op.drop_index("ix_digests_week_start", table_name="digests")
    op.drop_index("ix_digests_user_id", table_name="digests")
    op.drop_table("digests")

    op.drop_index("ix_signals_published_at", table_name="signals")
    op.drop_index("ix_signals_source_id", table_name="signals")
    op.drop_table("signals")

    op.drop_index("ix_sources_user_id", table_name="sources")
    op.drop_table("sources")

    op.drop_index("ix_users_email", table_name="users")
    op.drop_table("users")

    # Curated feeds/channels the pipeline pulls from. No user_id — sources are
    # global to the whole publication, not per-subscriber.
    op.create_table(
        "sources",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("url", sa.String(length=2048), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("category_hint", sa.String(length=64), nullable=True),
        sa.Column(
            "active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )
    op.create_index("ix_sources_url", "sources", ["url"], unique=True)
    op.create_index("ix_sources_active", "sources", ["active"])

    # Every article/post pulled from a source, before clustering.
    op.create_table(
        "raw_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("url", sa.String(length=2048), nullable=False),
        sa.Column("canonical_url", sa.String(length=2048), nullable=False),
        sa.Column("title", sa.String(length=1024), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("source_id", "canonical_url", name="uq_raw_items_source_canonical"),
    )
    op.create_index("ix_raw_items_source_id", "raw_items", ["source_id"])
    op.create_index("ix_raw_items_canonical_url", "raw_items", ["canonical_url"])
    op.create_index("ix_raw_items_first_seen_at", "raw_items", ["first_seen_at"])

    # A dedup group of raw_items that all cover the same story.
    op.create_table(
        "clusters",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("primary_url", sa.String(length=2048), nullable=False),
        sa.Column("category", sa.String(length=64), nullable=False),
        sa.Column("canonical_headline", sa.String(length=1024), nullable=False),
    )
    op.create_index("ix_clusters_category", "clusters", ["category"])
    op.create_index("ix_clusters_primary_url", "clusters", ["primary_url"])

    # A weekly issue of the digest. One row per ISO week.
    op.create_table(
        "issues",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("week_of", sa.Date(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="draft",
        ),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('draft', 'held', 'published')",
            name="ck_issues_status",
        ),
        sa.UniqueConstraint("week_of", name="uq_issues_week_of"),
    )
    op.create_index("ix_issues_status", "issues", ["status"])

    # A single item placed into an issue (one story, categorised and ordered).
    op.create_table(
        "items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("issue_id", sa.Integer(), nullable=False),
        sa.Column("cluster_id", sa.Integer(), nullable=False),
        sa.Column("category", sa.String(length=64), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("headline", sa.String(length=1024), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("primary_url", sa.String(length=2048), nullable=False),
        sa.Column(
            "extra_source_urls",
            sa.JSON(),
            nullable=False,
            server_default="[]",
        ),
        sa.ForeignKeyConstraint(["issue_id"], ["issues.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["cluster_id"], ["clusters.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("issue_id", "position", name="uq_items_issue_position"),
        sa.UniqueConstraint("issue_id", "cluster_id", name="uq_items_issue_cluster"),
    )
    op.create_index("ix_items_issue_id", "items", ["issue_id"])
    op.create_index("ix_items_cluster_id", "items", ["cluster_id"])
    op.create_index("ix_items_category", "items", ["category"])


def downgrade() -> None:
    # Drop the curated-digest tables, then rebuild the personal-aggregator
    # schema exactly as revisions 0001 and 0002 left it.
    op.drop_index("ix_items_category", table_name="items")
    op.drop_index("ix_items_cluster_id", table_name="items")
    op.drop_index("ix_items_issue_id", table_name="items")
    op.drop_table("items")

    op.drop_index("ix_issues_status", table_name="issues")
    op.drop_table("issues")

    op.drop_index("ix_clusters_primary_url", table_name="clusters")
    op.drop_index("ix_clusters_category", table_name="clusters")
    op.drop_table("clusters")

    op.drop_index("ix_raw_items_first_seen_at", table_name="raw_items")
    op.drop_index("ix_raw_items_canonical_url", table_name="raw_items")
    op.drop_index("ix_raw_items_source_id", table_name="raw_items")
    op.drop_table("raw_items")

    op.drop_index("ix_sources_active", table_name="sources")
    op.drop_index("ix_sources_url", table_name="sources")
    op.drop_table("sources")

    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("hashed_password", sa.String(length=255), nullable=False),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)

    op.create_table(
        "sources",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("url", sa.String(length=2048), nullable=False),
        sa.Column("title", sa.String(length=512), nullable=True),
        sa.Column(
            "type",
            sa.String(length=32),
            nullable=False,
            server_default="rss",
        ),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("user_id", "url", name="uq_sources_user_url"),
    )
    op.create_index("ix_sources_user_id", "sources", ["user_id"])

    op.create_table(
        "signals",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("guid", sa.String(length=512), nullable=False),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("url", sa.String(length=2048), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("source_id", "guid", name="uq_signals_source_guid"),
    )
    op.create_index("ix_signals_source_id", "signals", ["source_id"])
    op.create_index("ix_signals_published_at", "signals", ["published_at"])

    op.create_table(
        "digests",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("week_start", sa.Date(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("user_id", "week_start", name="uq_digests_user_week"),
    )
    op.create_index("ix_digests_user_id", "digests", ["user_id"])
    op.create_index("ix_digests_week_start", "digests", ["week_start"])

    op.create_table(
        "api_tokens",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_api_tokens_user_id", "api_tokens", ["user_id"])
    op.create_index("ix_api_tokens_token_hash", "api_tokens", ["token_hash"], unique=True)
