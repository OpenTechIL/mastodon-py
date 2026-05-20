"""`account_domain_blocks` — a per-account block against an entire domain.

Coarser-grained than per-account Block: when alice domain-blocks
`spam.example`, every account from that domain disappears from her
view (timelines + notifications + accounts list).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.python.db import Base


class AccountDomainBlock(Base):
    __tablename__ = "account_domain_blocks"
    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "domain",
            name="index_account_domain_blocks_on_account_id_and_domain",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("accounts.id"), nullable=False
    )
    domain: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
