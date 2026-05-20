"""Bearer-token resolution against `oauth_access_tokens`.

The contract every authenticated FastAPI router relies on:

    raw "abc123…" string from `Authorization: Bearer <token>`
       │
       ▼
    AuthContext(access_token, application, user|None, account|None)

A token is acceptable when it exists, is not revoked, is not past its
expiry, and (when bound to a resource owner) the owning user is
functional (confirmed, approved, not disabled). Otherwise we treat the
token as if it doesn't exist — the caller raises 401.

The streaming server already validates tokens against the same table
using equivalent rules; this resolver must remain consistent with that
contract or pushed events would diverge from REST responses.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.python.models import Account, OAuthAccessToken, OAuthApplication, User


@dataclass(slots=True)
class AuthContext:
    """The data every authenticated endpoint receives."""

    access_token: OAuthAccessToken
    application: OAuthApplication | None
    user: User | None
    account: Account | None

    @property
    def scopes(self) -> set[str]:
        return self.access_token.scope_list()

    def has_scope(self, required: str) -> bool:
        """OAuth scope check: a token has `read:statuses` iff it has either
        `read:statuses` directly or the parent `read` scope."""
        scopes = self.scopes
        if required in scopes:
            return True
        parent, _, _ = required.partition(":")
        return parent in scopes


async def resolve_bearer(
    session: AsyncSession,
    raw_token: str,
    *,
    now: datetime | None = None,
    client_ip: str | None = None,
) -> AuthContext | None:
    """Look up `raw_token` and return the auth context, or None to 401.

    On success this also fire-and-forgets a stamp of `last_used_at` /
    `last_used_ip` so the legacy admin UI's "active tokens" page keeps
    working while the cutover proceeds.
    """
    if not raw_token:
        return None

    stmt = select(OAuthAccessToken).where(OAuthAccessToken.token == raw_token).limit(1)
    access_token = (await session.execute(stmt)).scalar_one_or_none()
    if access_token is None:
        return None

    now = now or datetime.now(tz=UTC)
    if access_token.is_revoked() or access_token.is_expired(now=now):
        return None

    user = access_token.user
    if user is not None and not user.functional:
        return None

    account = user.account if user is not None else None
    await _stamp_last_used(session, access_token.id, now, client_ip)

    return AuthContext(
        access_token=access_token,
        application=access_token.application,
        user=user,
        account=account,
    )


async def _stamp_last_used(
    session: AsyncSession,
    token_id: int,
    now: datetime,
    client_ip: str | None,
) -> None:
    values: dict[str, object] = {"last_used_at": now.replace(tzinfo=None)}
    if client_ip:
        values["last_used_ip"] = client_ip
    await session.execute(update(OAuthAccessToken).where(OAuthAccessToken.id == token_id).values(**values))
