"""Tests for app registration + /oauth/token + /oauth/revoke."""

from __future__ import annotations

import time
from typing import Any

import bcrypt
import pyotp
import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.python.auth import otp as otp_module
from app.python.models import OAuthAccessToken, OAuthApplication


# ---------- /api/v1/apps ----------


@pytest.mark.asyncio
async def test_register_app_is_anonymous(client: AsyncClient) -> None:
    response = await client.post(
        "/api/v1/apps",
        json={
            "client_name": "Test Client",
            "redirect_uris": "https://example.test/callback",
            "scopes": "read write follow",
            "website": "https://example.test",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "Test Client"
    assert body["client_id"]
    assert body["client_secret"]
    assert body["scopes"] == ["read", "write", "follow"]
    # Doorkeeper-shaped credentials: 64 hex chars (= 32 bytes).
    assert len(body["client_id"]) == 64
    assert len(body["client_secret"]) == 64


@pytest.mark.asyncio
async def test_register_app_persists_row(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    response = await client.post(
        "/api/v1/apps", json={"client_name": "Persist Test"}
    )
    assert response.status_code == 200
    async with session_factory() as s:
        rows = (await s.execute(select(OAuthApplication))).scalars().all()
        assert len(rows) == 1
        assert rows[0].name == "Persist Test"
        # Default redirect_uri when caller omits it.
        assert rows[0].redirect_uri == "urn:ietf:wg:oauth:2.0:oob"
        # Default scopes.
        assert rows[0].scopes == "read"


@pytest.mark.asyncio
async def test_register_app_missing_name_is_422(client: AsyncClient) -> None:
    response = await client.post("/api/v1/apps", json={})
    assert response.status_code == 422


# ---------- /oauth/token ----------


async def _register(client: AsyncClient) -> tuple[str, str]:
    body = (
        await client.post("/api/v1/apps", json={"client_name": "Token Test"})
    ).json()
    return body["client_id"], body["client_secret"]


@pytest.mark.asyncio
async def test_client_credentials_grant(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    client_id, client_secret = await _register(client)
    response = await client.post(
        "/oauth/token",
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "Bearer"
    assert body["access_token"]
    assert body["scope"]
    assert body["created_at"] > 0

    async with session_factory() as s:
        rows = (await s.execute(select(OAuthAccessToken))).scalars().all()
        assert len(rows) == 1
        # Application-bound token (no resource owner).
        assert rows[0].resource_owner_id is None


@pytest.mark.asyncio
async def test_client_credentials_explicit_scope(
    client: AsyncClient,
) -> None:
    client_id, client_secret = await _register(client)
    response = await client.post(
        "/oauth/token",
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": "read:accounts",
        },
    )
    assert response.json()["scope"] == "read:accounts"


@pytest.mark.asyncio
async def test_bad_client_secret_is_401(client: AsyncClient) -> None:
    client_id, _ = await _register(client)
    response = await client.post(
        "/oauth/token",
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": "wrong-secret",
        },
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_unsupported_grant_type_is_400(client: AsyncClient) -> None:
    client_id, client_secret = await _register(client)
    response = await client.post(
        "/oauth/token",
        data={
            "grant_type": "password",  # not yet supported
            "client_id": client_id,
            "client_secret": client_secret,
        },
    )
    assert response.status_code == 400


# ---------- /oauth/revoke ----------


@pytest.mark.asyncio
async def test_revoke_marks_token_revoked(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    client_id, client_secret = await _register(client)
    token_response = await client.post(
        "/oauth/token",
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        },
    )
    access_token = token_response.json()["access_token"]

    response = await client.post(
        "/oauth/revoke",
        data={
            "token": access_token,
            "client_id": client_id,
            "client_secret": client_secret,
        },
    )
    assert response.status_code == 200

    async with session_factory() as s:
        row = (
            await s.execute(
                select(OAuthAccessToken).where(OAuthAccessToken.token == access_token)
            )
        ).scalar_one()
        assert row.revoked_at is not None


@pytest.mark.asyncio
async def test_revoke_unknown_token_returns_200(client: AsyncClient) -> None:
    """RFC 7009: don't leak token existence."""
    client_id, client_secret = await _register(client)
    response = await client.post(
        "/oauth/revoke",
        data={
            "token": "nonexistent-token",
            "client_id": client_id,
            "client_secret": client_secret,
        },
    )
    assert response.status_code == 200


# ---------- password grant ----------


def _hash(plaintext: str) -> str:
    return bcrypt.hashpw(plaintext.encode("utf-8"), bcrypt.gensalt(rounds=4)).decode("utf-8")


async def _seed_user_with_password(
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
    *,
    email: str = "alice@example.com",
    password: str = "hunter2",
    otp_secret: str | None = None,
    disabled: bool = False,
) -> None:
    async with session_factory() as s:
        s.add(seed_data["make_account"](id_=1, username="alice"))
        s.add(seed_data["make_account_stat"](account_id=1))
        s.add(
            seed_data["make_user"](
                id_=1,
                account_id=1,
                email=email,
                encrypted_password=_hash(password),
                otp_required_for_login=otp_secret is not None,
                otp_secret=otp_secret,
                disabled=disabled,
            )
        )
        await s.commit()


@pytest.mark.asyncio
async def test_password_grant_happy_path(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed_user_with_password(session_factory, seed_data)
    client_id, client_secret = await _register(client)

    response = await client.post(
        "/oauth/token",
        data={
            "grant_type": "password",
            "client_id": client_id,
            "client_secret": client_secret,
            "username": "alice@example.com",
            "password": "hunter2",
        },
    )
    assert response.status_code == 200
    assert response.json()["access_token"]

    async with session_factory() as s:
        tokens = (await s.execute(select(OAuthAccessToken))).scalars().all()
        # Most-recent token is the user-bound one we just minted.
        assert any(t.resource_owner_id == 1 for t in tokens)


@pytest.mark.asyncio
async def test_password_grant_unknown_email_is_401(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed_user_with_password(session_factory, seed_data)
    client_id, client_secret = await _register(client)
    response = await client.post(
        "/oauth/token",
        data={
            "grant_type": "password",
            "client_id": client_id,
            "client_secret": client_secret,
            "username": "ghost@example.com",
            "password": "hunter2",
        },
    )
    assert response.status_code == 401
    assert response.json()["detail"]["error"] == "invalid_grant"


@pytest.mark.asyncio
async def test_password_grant_wrong_password_is_401(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed_user_with_password(session_factory, seed_data)
    client_id, client_secret = await _register(client)
    response = await client.post(
        "/oauth/token",
        data={
            "grant_type": "password",
            "client_id": client_id,
            "client_secret": client_secret,
            "username": "alice@example.com",
            "password": "wrong",
        },
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_password_grant_disabled_user_is_401(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed_user_with_password(session_factory, seed_data, disabled=True)
    client_id, client_secret = await _register(client)
    response = await client.post(
        "/oauth/token",
        data={
            "grant_type": "password",
            "client_id": client_id,
            "client_secret": client_secret,
            "username": "alice@example.com",
            "password": "hunter2",
        },
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_password_grant_2fa_required_without_otp(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    secret = pyotp.random_base32()
    await _seed_user_with_password(
        session_factory, seed_data, otp_secret=secret
    )
    client_id, client_secret = await _register(client)
    response = await client.post(
        "/oauth/token",
        data={
            "grant_type": "password",
            "client_id": client_id,
            "client_secret": client_secret,
            "username": "alice@example.com",
            "password": "hunter2",
        },
    )
    assert response.status_code == 401
    assert response.json()["detail"]["error"] == "mfa_required"


@pytest.mark.asyncio
async def test_password_grant_2fa_with_wrong_code(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    secret = pyotp.random_base32()
    await _seed_user_with_password(
        session_factory, seed_data, otp_secret=secret
    )
    client_id, client_secret = await _register(client)
    response = await client.post(
        "/oauth/token",
        data={
            "grant_type": "password",
            "client_id": client_id,
            "client_secret": client_secret,
            "username": "alice@example.com",
            "password": "hunter2",
            "otp_attempt": "000000",
        },
    )
    assert response.status_code == 401
    assert response.json()["detail"]["error"] == "invalid_grant"


@pytest.mark.asyncio
async def test_password_grant_2fa_with_correct_code(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixed = 1_700_000_000.0
    monkeypatch.setattr(otp_module, "_now", lambda: fixed)
    secret = pyotp.random_base32()
    code = pyotp.TOTP(secret).at(int(fixed))

    await _seed_user_with_password(
        session_factory, seed_data, otp_secret=secret
    )
    client_id, client_secret = await _register(client)

    response = await client.post(
        "/oauth/token",
        data={
            "grant_type": "password",
            "client_id": client_id,
            "client_secret": client_secret,
            "username": "alice@example.com",
            "password": "hunter2",
            "otp_attempt": code,
        },
    )
    assert response.status_code == 200
    assert response.json()["access_token"]


@pytest.mark.asyncio
async def test_revoked_token_no_longer_authenticates(
    client: AsyncClient,
) -> None:
    """End-to-end: revoke flips the token so the protected endpoint 401s."""
    # Register an app, get a token, hit a protected endpoint, revoke, retry.
    client_id, client_secret = await _register(client)
    tr = await client.post(
        "/oauth/token",
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        },
    )
    raw = tr.json()["access_token"]

    # verify_credentials on /api/v1/apps reads the application from the token
    # — works because client_credentials tokens have an application_id set.
    ok = await client.get(
        "/api/v1/apps/verify_credentials",
        headers={"Authorization": f"Bearer {raw}"},
    )
    assert ok.status_code == 200

    await client.post(
        "/oauth/revoke",
        data={
            "token": raw,
            "client_id": client_id,
            "client_secret": client_secret,
        },
    )

    after = await client.get(
        "/api/v1/apps/verify_credentials",
        headers={"Authorization": f"Bearer {raw}"},
    )
    assert after.status_code == 401
