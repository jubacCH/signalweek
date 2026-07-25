"""Thin repository classes wrapping SQLAlchemy ``Session`` operations.

Repositories flush new rows so callers can read back generated IDs, but leave
commit/rollback to the caller (via ``session_scope`` or explicit control).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from signalweek.db.models import Digest, Signal, Source, User


class UserRepository:
    """CRUD helpers for :class:`User`."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def create(self, *, email: str, hashed_password: str, is_active: bool = True) -> User:
        user = User(email=email, hashed_password=hashed_password, is_active=is_active)
        self.session.add(user)
        self.session.flush()
        return user

    def get(self, user_id: int) -> User | None:
        return self.session.get(User, user_id)

    def get_by_email(self, email: str) -> User | None:
        stmt = select(User).where(User.email == email)
        return self.session.execute(stmt).scalar_one_or_none()

    def list(self) -> Sequence[User]:
        stmt = select(User).order_by(User.id)
        return self.session.execute(stmt).scalars().all()


class SourceRepository:
    """CRUD helpers for :class:`Source`."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def create(
        self,
        *,
        user_id: int,
        url: str,
        title: str | None = None,
        type: str = "rss",
        is_active: bool = True,
    ) -> Source:
        source = Source(
            user_id=user_id,
            url=url,
            title=title,
            type=type,
            is_active=is_active,
        )
        self.session.add(source)
        self.session.flush()
        return source

    def get(self, source_id: int) -> Source | None:
        return self.session.get(Source, source_id)

    def list_for_user(self, user_id: int) -> Sequence[Source]:
        stmt = select(Source).where(Source.user_id == user_id).order_by(Source.id)
        return self.session.execute(stmt).scalars().all()

    def list_active(self) -> Sequence[Source]:
        stmt = select(Source).where(Source.is_active.is_(True)).order_by(Source.id)
        return self.session.execute(stmt).scalars().all()


class SignalRepository:
    """CRUD helpers for :class:`Signal`."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def create(
        self,
        *,
        source_id: int,
        guid: str,
        title: str,
        url: str,
        summary: str | None = None,
        published_at: datetime | None = None,
    ) -> Signal:
        signal = Signal(
            source_id=source_id,
            guid=guid,
            title=title,
            url=url,
            summary=summary,
            published_at=published_at,
        )
        self.session.add(signal)
        self.session.flush()
        return signal

    def get(self, signal_id: int) -> Signal | None:
        return self.session.get(Signal, signal_id)

    def get_by_guid(self, source_id: int, guid: str) -> Signal | None:
        stmt = select(Signal).where(Signal.source_id == source_id, Signal.guid == guid)
        return self.session.execute(stmt).scalar_one_or_none()

    def list_for_source(self, source_id: int) -> Sequence[Signal]:
        stmt = (
            select(Signal)
            .where(Signal.source_id == source_id)
            .order_by(Signal.published_at.desc().nulls_last(), Signal.id.desc())
        )
        return self.session.execute(stmt).scalars().all()


class DigestRepository:
    """CRUD helpers for :class:`Digest`."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def create(
        self,
        *,
        user_id: int,
        week_start: date,
        content: str,
        sent_at: datetime | None = None,
    ) -> Digest:
        digest = Digest(
            user_id=user_id,
            week_start=week_start,
            content=content,
            sent_at=sent_at,
        )
        self.session.add(digest)
        self.session.flush()
        return digest

    def get(self, digest_id: int) -> Digest | None:
        return self.session.get(Digest, digest_id)

    def get_for_week(self, user_id: int, week_start: date) -> Digest | None:
        stmt = select(Digest).where(Digest.user_id == user_id, Digest.week_start == week_start)
        return self.session.execute(stmt).scalar_one_or_none()

    def list_for_user(self, user_id: int) -> Sequence[Digest]:
        stmt = select(Digest).where(Digest.user_id == user_id).order_by(Digest.week_start.desc())
        return self.session.execute(stmt).scalars().all()
