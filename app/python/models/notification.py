"""`notifications` row.

The first ported polymorphic association — `(activity_id, activity_type)`
points at one of several concrete row types depending on `type`. The
ORM doesn't resolve the polymorphic load automatically here: each
relationship is keyed on a constant `activity_type` discriminator value,
and the serializer asks for the one that matches `type`. This is the
pattern the plan calls out as the replacement for AR's
`belongs_to :activity, polymorphic: true`.

The set of `activity_type` strings is exactly the Ruby class names the
legacy backend writes — `Favourite`, `Status`, `Follow`, `FollowRequest`,
`Mention`, `Poll`, `Report`, etc. Cross-backend rows must carry the same
strings while both apps are running, so we hard-code these values.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.python.db import Base

if TYPE_CHECKING:
    pass


class NotificationType(StrEnum):
    """Subset of Mastodon's notification types we generate today.

    More types (`mention`, `poll`, `update`, `admin.sign_up`, `status`,
    `quote`, `severed_relationships`, `moderation_warning`,
    `added_to_collection`, etc.) land alongside their producer phases.
    """

    FAVOURITE = "favourite"
    REBLOG = "reblog"
    FOLLOW = "follow"
    FOLLOW_REQUEST = "follow_request"
    MENTION = "mention"


# Mapping from notification type to the Rails class name we must write
# into `activity_type`. Both backends read this column during cutover.
ACTIVITY_TYPE_FOR: dict[NotificationType, str] = {
    NotificationType.FAVOURITE: "Favourite",
    NotificationType.REBLOG: "Status",  # the boost wrapper Status row
    NotificationType.FOLLOW: "Follow",
    NotificationType.FOLLOW_REQUEST: "FollowRequest",
    NotificationType.MENTION: "Mention",
}


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    account_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("accounts.id"), nullable=False
    )
    from_account_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("accounts.id"), nullable=False
    )

    activity_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    activity_type: Mapped[str] = mapped_column(String, nullable=False)

    type: Mapped[str | None] = mapped_column(String, nullable=True)
    filtered: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    group_key: Mapped[str | None] = mapped_column(String, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)

    # The `from_account` is the actor — who favourited, who followed.
    # `account` is the recipient and is rarely loaded with the
    # notification list (it's always the viewer); we don't declare it
    # to avoid spurious joins.
    from_account = relationship(
        "Account",
        primaryjoin="Notification.from_account_id == Account.id",
        foreign_keys=lambda: [Notification.from_account_id],
        lazy="joined",
        viewonly=True,
    )

    @property
    def notification_type(self) -> NotificationType | None:
        if self.type is None:
            return None
        try:
            return NotificationType(self.type)
        except ValueError:
            return None  # An unsupported (yet-to-port) type — render as-is.

    @property
    def status_typed(self) -> bool:
        """Whether the API should surface the `status` field for this row."""
        return self.notification_type in (
            NotificationType.FAVOURITE,
            NotificationType.REBLOG,
            NotificationType.MENTION,
        )
