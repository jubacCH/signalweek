"""Dataclasses describing a rendered-ready weekly digest.

The digest model is deliberately decoupled from the ORM and from the ranking
module so the renderers can be exercised with hand-built inputs in snapshot
tests without any database or scoring machinery in the way.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class DigestItem:
    """A single ranked signal ready to be rendered inside a digest section."""

    title: str
    url: str
    summary: str | None = None
    published_at: datetime | None = None
    score: float = 0.0


@dataclass(frozen=True)
class DigestSection:
    """A grouping of ranked items under a common source header."""

    source_title: str
    source_url: str
    items: tuple[DigestItem, ...] = ()


@dataclass(frozen=True)
class Digest:
    """A user's weekly digest — a window plus zero or more sections."""

    user_email: str
    window_start: datetime
    window_end: datetime
    sections: tuple[DigestSection, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.sections
