"""ORM models mapped against the existing Postgres schema.

Models are added as each phase ports the subsystem that owns them.
Currently exposed:

  - Account                (subset of `accounts` columns used by auth)
  - User                   (full `users` columns needed by auth + OTP)
  - OAuthApplication       (`oauth_applications`)
  - OAuthAccessToken       (`oauth_access_tokens`)
"""

from __future__ import annotations

from app.python.models.account import Account
from app.python.models.account_conversation import AccountConversation
from app.python.models.account_domain_block import AccountDomainBlock
from app.python.models.account_note import AccountNote
from app.python.models.account_pin import AccountPin
from app.python.models.account_stat import AccountStat
from app.python.models.announcement import Announcement
from app.python.models.announcement_mute import AnnouncementMute
from app.python.models.block import Block
from app.python.models.bookmark import Bookmark
from app.python.models.conversation import Conversation
from app.python.models.custom_emoji import CustomEmoji
from app.python.models.custom_filter import (
    VALID_CONTEXTS,
    CustomFilter,
    FilterAction,
    parse_filter_action,
)
from app.python.models.custom_filter_keyword import CustomFilterKeyword
from app.python.models.custom_filter_status import CustomFilterStatus
from app.python.models.favourite import Favourite
from app.python.models.featured_tag import FeaturedTag
from app.python.models.follow import Follow
from app.python.models.follow_request import FollowRequest
from app.python.models.list import List, RepliesPolicy, parse_replies_policy
from app.python.models.list_account import ListAccount
from app.python.models.marker import Marker
from app.python.models.media_attachment import (
    MediaAttachment,
    MediaProcessing,
    MediaType,
)
from app.python.models.mention import Mention
from app.python.models.mute import Mute
from app.python.models.notification import ACTIVITY_TYPE_FOR, Notification, NotificationType
from app.python.models.oauth_access_token import OAuthAccessToken
from app.python.models.poll import Poll
from app.python.models.poll_vote import PollVote
from app.python.models.oauth_application import OAuthApplication
from app.python.models.report import Report, ReportCategory, parse_report_category
from app.python.models.status import Status, Visibility
from app.python.models.status_edit import StatusEdit
from app.python.models.status_pin import StatusPin
from app.python.models.status_stat import StatusStat
from app.python.models.status_tag import StatusTag
from app.python.models.tag import Tag
from app.python.models.tag_follow import TagFollow
from app.python.models.user import User

__all__ = [
    "ACTIVITY_TYPE_FOR",
    "Account",
    "AccountConversation",
    "AccountDomainBlock",
    "AccountNote",
    "AccountPin",
    "AccountStat",
    "Announcement",
    "AnnouncementMute",
    "Block",
    "Bookmark",
    "Conversation",
    "CustomEmoji",
    "CustomFilter",
    "CustomFilterKeyword",
    "CustomFilterStatus",
    "FilterAction",
    "VALID_CONTEXTS",
    "parse_filter_action",
    "Favourite",
    "FeaturedTag",
    "Follow",
    "FollowRequest",
    "List",
    "ListAccount",
    "Marker",
    "MediaAttachment",
    "MediaProcessing",
    "MediaType",
    "Mention",
    "Mute",
    "RepliesPolicy",
    "parse_replies_policy",
    "Notification",
    "NotificationType",
    "OAuthAccessToken",
    "OAuthApplication",
    "Poll",
    "PollVote",
    "Report",
    "ReportCategory",
    "parse_report_category",
    "Status",
    "StatusEdit",
    "StatusPin",
    "StatusStat",
    "StatusTag",
    "Tag",
    "TagFollow",
    "User",
    "Visibility",
]
