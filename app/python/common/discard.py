"""Soft-delete mixin.

Models with a nullable `deleted_at` column mix in `Discardable` and
callers compose `Discardable.kept(select(Status))` explicitly. We
deliberately do not install a default scope or ORM event that hides
discarded rows automatically — implicit filtering is the bug class
this module is designed to avoid.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Select
from sqlalchemy.orm import Mapped, mapped_column

if TYPE_CHECKING:
    from sqlalchemy.sql.elements import ColumnElement


class Discardable:
    """Mixin providing `deleted_at` plus query helpers."""

    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    @classmethod
    def kept_clause(cls) -> ColumnElement[bool]:
        return cls.deleted_at.is_(None)

    @classmethod
    def discarded_clause(cls) -> ColumnElement[bool]:
        return cls.deleted_at.is_not(None)

    @classmethod
    def kept(cls, stmt: Select) -> Select:
        return stmt.where(cls.kept_clause())

    @classmethod
    def filter_discarded(cls, stmt: Select) -> Select:
        return stmt.where(cls.discarded_clause())

    @property
    def discarded(self) -> bool:
        return self.deleted_at is not None

    def discard(self) -> None:
        if self.deleted_at is None:
            self.deleted_at = datetime.now(tz=UTC)

    def undiscard(self) -> None:
        self.deleted_at = None
