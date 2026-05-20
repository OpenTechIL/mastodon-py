"""Bearer-token resolution against the DB."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.python.auth.tokens import resolve_bearer
from app.python.models import OAuthAccessToken


@pytest.mark.asyncio
async def test_resolves_valid_token(
    session: AsyncSession,
    seed_data: dict[str, Any],
    fixed_now: datetime,
) -> None:
    session.add_all(
        [
            seed_data["make_account"](),
            seed_data["make_user"](),
            seed_data["make_application"](),
            seed_data["make_token"](),
        ]
    )
    await session.commit()

    ctx = await resolve_bearer(session, "raw-token-abc", now=fixed_now, client_ip="1.2.3.4")

    assert ctx is not None
    assert ctx.user is not None and ctx.user.id == 1
    assert ctx.account is not None and ctx.account.username == "alice"
    assert ctx.application is not None and ctx.application.name == "Test Client"
    assert ctx.has_scope("read")
    assert ctx.has_scope("read:statuses")  # parent-scope inheritance
    assert not ctx.has_scope("admin")


@pytest.mark.asyncio
async def test_unknown_token_returns_none(session: AsyncSession) -> None:
    assert await resolve_bearer(session, "no-such-token") is None


@pytest.mark.asyncio
async def test_empty_token_returns_none(session: AsyncSession) -> None:
    assert await resolve_bearer(session, "") is None


@pytest.mark.asyncio
async def test_revoked_token_rejected(
    session: AsyncSession,
    seed_data: dict[str, Any],
    fixed_now: datetime,
    naive_now: datetime,
) -> None:
    session.add_all(
        [
            seed_data["make_account"](),
            seed_data["make_user"](),
            seed_data["make_application"](),
            seed_data["make_token"](revoked_at=naive_now - timedelta(minutes=1)),
        ]
    )
    await session.commit()

    assert await resolve_bearer(session, "raw-token-abc", now=fixed_now) is None


@pytest.mark.asyncio
async def test_expired_token_rejected(
    session: AsyncSession,
    seed_data: dict[str, Any],
    fixed_now: datetime,
    naive_now: datetime,
) -> None:
    session.add_all(
        [
            seed_data["make_account"](),
            seed_data["make_user"](),
            seed_data["make_application"](),
            seed_data["make_token"](
                created_at=naive_now - timedelta(hours=2),
                expires_in=3600,
            ),
        ]
    )
    await session.commit()

    assert await resolve_bearer(session, "raw-token-abc", now=fixed_now) is None


@pytest.mark.asyncio
async def test_disabled_user_rejected(
    session: AsyncSession,
    seed_data: dict[str, Any],
    fixed_now: datetime,
) -> None:
    session.add_all(
        [
            seed_data["make_account"](),
            seed_data["make_user"](disabled=True),
            seed_data["make_application"](),
            seed_data["make_token"](),
        ]
    )
    await session.commit()

    assert await resolve_bearer(session, "raw-token-abc", now=fixed_now) is None


@pytest.mark.asyncio
async def test_unconfirmed_user_rejected(
    session: AsyncSession,
    seed_data: dict[str, Any],
    fixed_now: datetime,
) -> None:
    session.add_all(
        [
            seed_data["make_account"](),
            seed_data["make_user"](confirmed_at=None),
            seed_data["make_application"](),
            seed_data["make_token"](),
        ]
    )
    await session.commit()

    assert await resolve_bearer(session, "raw-token-abc", now=fixed_now) is None


@pytest.mark.asyncio
async def test_client_credentials_token_has_no_user(
    session: AsyncSession,
    seed_data: dict[str, Any],
    fixed_now: datetime,
) -> None:
    session.add_all(
        [
            seed_data["make_application"](),
            seed_data["make_token"](resource_owner_id=None, scopes="read"),
        ]
    )
    await session.commit()

    ctx = await resolve_bearer(session, "raw-token-abc", now=fixed_now)
    assert ctx is not None
    assert ctx.user is None
    assert ctx.account is None
    assert ctx.application is not None


@pytest.mark.asyncio
async def test_last_used_stamped_on_success(
    session: AsyncSession,
    seed_data: dict[str, Any],
    fixed_now: datetime,
) -> None:
    session.add_all(
        [
            seed_data["make_account"](),
            seed_data["make_user"](),
            seed_data["make_application"](),
            seed_data["make_token"](),
        ]
    )
    await session.commit()

    await resolve_bearer(session, "raw-token-abc", now=fixed_now, client_ip="9.9.9.9")
    await session.commit()

    refreshed = (
        await session.execute(select(OAuthAccessToken).where(OAuthAccessToken.id == 1))
    ).scalar_one()
    assert refreshed.last_used_at == fixed_now.replace(tzinfo=None)
    assert refreshed.last_used_ip == "9.9.9.9"
