"""Declarative base for the signalweek ORM models."""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Root declarative class shared by every ORM model."""
