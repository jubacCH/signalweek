"""ORM models for the signalweek data layer."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from signalweek.db.base import Base


class Issue(Base):
    """A weekly signalweek digest issue."""

    __tablename__ = "issues"

    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[int] = mapped_column(unique=True, index=True)
    title: Mapped[str] = mapped_column(String(200))
    body_markdown: Mapped[str] = mapped_column(Text(), default="")
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    items: Mapped[list[SignalItem]] = relationship(
        back_populates="issue",
        order_by="SignalItem.id",
        lazy="selectin",
    )


class SignalItem(Base):
    """A single link / signal ingested from an upstream source."""

    __tablename__ = "signal_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(300))
    url: Mapped[str] = mapped_column(String(2000))
    source: Mapped[str | None] = mapped_column(String(200))
    summary: Mapped[str | None] = mapped_column(Text())
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    issue_id: Mapped[int | None] = mapped_column(
        ForeignKey("issues.id", ondelete="SET NULL"), index=True
    )
    issue: Mapped[Issue | None] = relationship(back_populates="items")

    __table_args__ = (UniqueConstraint("url", name="uq_signal_items_url"),)
