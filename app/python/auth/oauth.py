"""OAuth issuance — the half of Phase 1 deferred from the validation slice.

This module currently covers:

  - App registration (`POST /api/v1/apps`): create an `OAuthApplication`
    with a freshly generated `uid` (client_id) and `secret`. Anonymous,
    matching the legacy controller's `skip_before_action`.
  - `client_credentials` grant: a bearer token bound to the application
    only, with `resource_owner_id=null`. Useful for endpoints that need
    *some* token but don't need a user (e.g. `GET /api/v1/timelines/public`
    when the instance requires auth).
  - Token revocation.

Explicitly deferred to a later slice:

  - `authorization_code` grant — needs the consent UI (Devise HTML page)
    and the `oauth_access_grants` write path.
  - `password` grant — needs Devise password verification + 2FA
    interactive flow. We *have* the underlying primitives in
    `auth.passwords` and `auth.otp`; the missing piece is the
    grant-shape orchestration around them.
  - `refresh_token` rotation.
  - `/oauth/introspect` and `/oauth/userinfo` (OIDC subset).
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.python.auth import otp as otp_module
from app.python.auth import passwords
from app.python.common.snowflake import now_id
from app.python.models import OAuthAccessToken, OAuthApplication, User


DEFAULT_SCOPES = "read"  # Doorkeeper default in legacy config.


class InvalidClient(Exception):
    """Raised when client_id + client_secret don't match a known app."""


class InvalidGrant(Exception):
    """Raised on bad username/password or revoked/disabled user."""


class MFARequired(Exception):
    """Raised when password is correct but the user has 2FA enabled and
    no `otp_attempt` was supplied. The legacy backend returns this as a
    401 with `error_description: "Missing 2FA code"` and a sentinel
    error string the React client checks for."""


def _generate_credential() -> str:
    """Doorkeeper uses `SecureRandom.hex(32)` (32 random bytes -> 64 hex chars)."""
    return secrets.token_hex(32)


async def register_application(
    session: AsyncSession,
    *,
    client_name: str,
    redirect_uri: str,
    scopes: str | None,
    website: str | None,
) -> OAuthApplication:
    now = datetime.now(tz=timezone.utc).replace(tzinfo=None)
    row = OAuthApplication(
        id=now_id(),
        name=client_name,
        uid=_generate_credential(),
        secret=_generate_credential(),
        redirect_uri=redirect_uri,
        scopes=scopes or DEFAULT_SCOPES,
        website=website,
        confidential=True,
        superapp=False,
        owner_id=None,
        owner_type=None,
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    await session.commit()
    return row


async def _resolve_client(
    session: AsyncSession, client_id: str, client_secret: str
) -> OAuthApplication:
    row = (
        await session.execute(
            select(OAuthApplication).where(OAuthApplication.uid == client_id)
        )
    ).scalar_one_or_none()
    if row is None or not secrets.compare_digest(row.secret, client_secret):
        raise InvalidClient
    return row


async def client_credentials_grant(
    session: AsyncSession,
    *,
    client_id: str,
    client_secret: str,
    scope: str | None,
) -> OAuthAccessToken:
    """Mint a token bound to the application (no resource owner)."""
    app = await _resolve_client(session, client_id, client_secret)
    now = datetime.now(tz=timezone.utc).replace(tzinfo=None)
    token = OAuthAccessToken(
        id=now_id(),
        token=_generate_credential(),
        refresh_token=None,
        scopes=scope or app.scopes,
        application_id=app.id,
        resource_owner_id=None,
        expires_in=None,
        revoked_at=None,
        created_at=now,
        last_used_at=None,
        last_used_ip=None,
    )
    session.add(token)
    await session.commit()
    return token


async def password_grant(
    session: AsyncSession,
    *,
    client_id: str,
    client_secret: str,
    username: str,
    password: str,
    otp_attempt: str | None,
    scope: str | None,
) -> OAuthAccessToken:
    """Password grant. Devise uses email as the login identifier."""
    app = await _resolve_client(session, client_id, client_secret)

    user = (
        await session.execute(
            select(User).where(User.email == username).limit(1)
        )
    ).scalar_one_or_none()
    if user is None or not passwords.verify(password, user.encrypted_password):
        raise InvalidGrant
    if not user.functional:
        raise InvalidGrant

    if user.otp_enabled:
        if not otp_attempt:
            raise MFARequired
        result = otp_module.verify(
            user.otp_secret,
            otp_attempt,
            previous_timestep=user.consumed_timestep,
        )
        if not result.valid:
            raise InvalidGrant
        # Advance the replay-protection cursor so the same code can't be
        # reused within the drift window.
        user.consumed_timestep = result.consumed_timestep

    now = datetime.now(tz=timezone.utc).replace(tzinfo=None)
    token = OAuthAccessToken(
        id=now_id(),
        token=_generate_credential(),
        refresh_token=None,
        scopes=scope or app.scopes,
        application_id=app.id,
        resource_owner_id=user.id,
        expires_in=None,
        revoked_at=None,
        created_at=now,
        last_used_at=None,
        last_used_ip=None,
    )
    session.add(token)
    await session.commit()
    return token


async def revoke_token(
    session: AsyncSession,
    *,
    raw_token: str,
    client_id: str | None,
    client_secret: str | None,
) -> bool:
    """Revoke an access token by setting `revoked_at`.

    Per RFC 7009 the endpoint returns 200 even when the token doesn't
    exist — callers shouldn't be able to probe token existence via the
    revoke endpoint. We do require the caller to supply a valid
    client_id + client_secret so anonymous probing isn't possible.
    """
    if client_id and client_secret:
        try:
            await _resolve_client(session, client_id, client_secret)
        except InvalidClient:
            return False

    row = (
        await session.execute(
            select(OAuthAccessToken).where(OAuthAccessToken.token == raw_token)
        )
    ).scalar_one_or_none()
    if row is None:
        return True  # masquerade as success per RFC 7009
    if row.revoked_at is not None:
        return True
    row.revoked_at = datetime.now(tz=timezone.utc).replace(tzinfo=None)
    await session.commit()
    return True
