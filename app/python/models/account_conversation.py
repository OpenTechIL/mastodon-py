"""`account_conversations` — per-account view of a Conversation.

Each (account, conversation, participant-set) triple gets one row.
`status_ids` is the array of direct statuses in this thread that the
account participates in; `last_status_id` is the most recent. `unread`
flips to false when the account calls `POST /api/v1/conversations/{id}/read`.
"""

from __future__ import annotations

from sqlalchemy import ARRAY, BigInteger, Boolean, ForeignKey, Integer, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.python.db import Base

_PG_OR_JSON_BIGINT_ARRAY = ARRAY(BigInteger).with_variant(JSON(), "sqlite")


class AccountConversation(Base):
    __tablename__ = "account_conversations"
    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "conversation_id",
            "participant_account_ids",
            name="index_unique_conversations",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("accounts.id"), nullable=False
    )
    conversation_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("conversations.id"), nullable=False
    )
    last_status_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    lock_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    participant_account_ids: Mapped[list[int]] = mapped_column(
        _PG_OR_JSON_BIGINT_ARRAY, nullable=False, default=list
    )
    status_ids: Mapped[list[int]] = mapped_column(
        _PG_OR_JSON_BIGINT_ARRAY, nullable=False, default=list
    )
    unread: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
