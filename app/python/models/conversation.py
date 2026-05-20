"""`conversations` row.

A DM thread. Statuses with `visibility=direct` typically share a single
Conversation when they're replies to one another; new threads create new
Conversation rows. `parent_status_id` and `parent_account_id` link back
to the post that started the thread; `uri` is the federated URI for
remote-origin conversations.

The legacy backend keys conversations by sorted participant set so two
DM threads between the same people share the same Conversation row.
We replicate that in `services/conversations.py` rather than the model.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.python.db import Base


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    uri: Mapped[str | None] = mapped_column(String, nullable=True)
    parent_status_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    parent_account_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
