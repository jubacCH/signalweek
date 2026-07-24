"""Insert/select smoke tests for the ORM models."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from signalweek.db import Issue, SignalItem, Subscriber


async def test_insert_and_select_issue(session: AsyncSession) -> None:
    session.add(
        Issue(
            number=1,
            title="Week One",
            published_at=datetime(2026, 7, 24, tzinfo=UTC),
        )
    )
    await session.commit()

    result = await session.execute(select(Issue).where(Issue.number == 1))
    issue = result.scalar_one()

    assert issue.title == "Week One"
    assert issue.id is not None
    assert issue.created_at is not None


async def test_insert_and_select_signal_item_with_issue(session: AsyncSession) -> None:
    issue = Issue(number=2, title="Week Two")
    item = SignalItem(
        title="Interesting link",
        url="https://example.com/post",
        source="example.com",
        summary="A short summary",
        issue=issue,
    )
    session.add(item)
    await session.commit()

    fetched = (
        await session.execute(
            select(SignalItem).where(SignalItem.url == "https://example.com/post")
        )
    ).scalar_one()

    assert fetched.title == "Interesting link"
    assert fetched.issue is not None
    assert fetched.issue.number == 2


async def test_signal_item_url_is_unique(session: AsyncSession) -> None:
    session.add(SignalItem(title="A", url="https://dup.example/x"))
    await session.commit()

    session.add(SignalItem(title="B", url="https://dup.example/x"))
    try:
        await session.commit()
    except Exception:
        await session.rollback()
    else:  # pragma: no cover - defensive: uniqueness should have triggered
        raise AssertionError("expected uniqueness violation on SignalItem.url")


async def test_insert_and_select_subscriber(session: AsyncSession) -> None:
    session.add(
        Subscriber(
            email="reader@example.com",
            confirmed_at=datetime(2026, 7, 20, tzinfo=UTC),
        )
    )
    await session.commit()

    subscriber = (
        await session.execute(
            select(Subscriber).where(Subscriber.email == "reader@example.com")
        )
    ).scalar_one()

    assert subscriber.confirmed_at is not None
    assert subscriber.unsubscribed_at is None
    assert subscriber.created_at is not None
