"""`statuses_tags` join row — composite (status_id, tag_id) PK."""

from __future__ import annotations

from sqlalchemy import BigInteger, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.python.db import Base


class StatusTag(Base):
    __tablename__ = "statuses_tags"

    # Schema declares composite PK (tag_id, status_id) in that order, but
    # SQLAlchemy's declarative mapper just needs a `primary_key=True` on each
    # member of the key — the actual column order in the constraint matches
    # the schema by virtue of the column declaration order below.
    tag_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("tags.id"), primary_key=True
    )
    status_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("statuses.id"), primary_key=True
    )
