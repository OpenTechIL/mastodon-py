"""REST shape for Account.

Mirrors `REST::AccountSerializer` for the subset of fields the React
frontend reads on a profile/timeline render. Deferred fields (roles,
moved, fields-with-verified-at, suspension/limited flags, indexable
controls) emit safe defaults and are filled in alongside their owning
features.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.python.lib.asset_urls import account_uri, account_url, avatar_url, header_url
from app.python.lib.html import account_bio_format
from app.python.models import Account


class AccountField(BaseModel):
    name: str
    value: str
    verified_at: datetime | None = None


class Account_(BaseModel):
    """Public account shape. Class name has trailing underscore because the
    Pydantic class collides with the SQLAlchemy `Account` model otherwise.
    The wire output uses no name."""

    model_config = ConfigDict(populate_by_name=True)

    id: str
    username: str
    acct: str
    display_name: str
    locked: bool
    bot: bool
    discoverable: bool | None
    indexable: bool
    group: bool
    created_at: datetime
    note: str
    url: str
    uri: str
    avatar: str
    avatar_static: str
    header: str
    header_static: str
    followers_count: int
    following_count: int
    statuses_count: int
    last_status_at: datetime | None

    # Composite/derived fields whose data sources land later.
    fields: list[AccountField] = Field(default_factory=list)
    emojis: list[Any] = Field(default_factory=list)
    # Credential-only fields (None for public account lookups)
    source: dict[str, Any] | None = None
    role: dict[str, Any] | None = None


def serialize_account(account: Account) -> Account_:
    stat = account.stat
    return Account_(
        id=str(account.id),
        username=account.username,
        acct=account.acct,
        display_name=account.display_name,
        locked=account.locked,
        bot=account.bot,
        discoverable=account.discoverable,
        indexable=account.indexable,
        group=account.group,
        created_at=account.created_at,
        note=account_bio_format(account.note),
        url=account_url(account),
        uri=account_uri(account),
        avatar=avatar_url(account),
        avatar_static=avatar_url(account, static=True),
        header=header_url(account),
        header_static=header_url(account, static=True),
        followers_count=stat.followers_count if stat else 0,
        following_count=stat.following_count if stat else 0,
        statuses_count=stat.statuses_count if stat else 0,
        last_status_at=stat.last_status_at if stat else None,
        fields=[
            AccountField(name=str(f.get("name", "")), value=str(f.get("value", "")))
            for f in (account.fields or [])
        ],
        emojis=[],
    )
