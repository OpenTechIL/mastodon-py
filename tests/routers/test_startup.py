"""Tests for /custom_emojis, /markers, /preferences."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.python.models import CustomEmoji, Marker

_AUTH = {"Authorization": "Bearer raw-token-abc"}


async def _seed_user(
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    async with session_factory() as s:
        s.add_all(
            [
                seed_data["make_account"](id_=1, username="alice"),
                seed_data["make_account_stat"](account_id=1),
                seed_data["make_user"](id_=1, account_id=1),
                seed_data["make_application"](),
                seed_data["make_token"](),
            ]
        )
        await s.commit()


# ---------- custom_emojis ----------


@pytest.mark.asyncio
async def test_custom_emojis_filters_disabled_and_hidden(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    ts = datetime.now(tz=UTC).replace(tzinfo=None)
    async with session_factory() as s:
        s.add_all(
            [
                CustomEmoji(
                    id=1,
                    shortcode="party",
                    domain=None,
                    image_file_name="party.png",
                    image_remote_url=None,
                    visible_in_picker=True,
                    disabled=False,
                    created_at=ts,
                    updated_at=ts,
                ),
                CustomEmoji(
                    id=2,
                    shortcode="hidden",
                    domain=None,
                    image_file_name="hidden.png",
                    image_remote_url=None,
                    visible_in_picker=False,  # excluded
                    disabled=False,
                    created_at=ts,
                    updated_at=ts,
                ),
                CustomEmoji(
                    id=3,
                    shortcode="banned",
                    domain=None,
                    image_file_name="banned.png",
                    image_remote_url=None,
                    visible_in_picker=True,
                    disabled=True,  # excluded
                    created_at=ts,
                    updated_at=ts,
                ),
            ]
        )
        await s.commit()

    response = await client.get("/api/v1/custom_emojis")
    assert response.status_code == 200
    body = response.json()
    assert [e["shortcode"] for e in body] == ["party"]
    assert body[0]["url"].endswith("/system/custom_emojis/images/1/original/party.png")


@pytest.mark.asyncio
async def test_custom_emojis_anonymous(client: AsyncClient) -> None:
    """No auth required."""
    response = await client.get("/api/v1/custom_emojis")
    assert response.status_code == 200


# ---------- markers ----------


@pytest.mark.asyncio
async def test_markers_get_returns_only_requested_timelines(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed_user(session_factory, seed_data)
    ts = datetime.now(tz=UTC).replace(tzinfo=None)
    async with session_factory() as s:
        s.add(
            Marker(
                id=10,
                user_id=1,
                timeline="home",
                last_read_id=12345,
                lock_version=3,
                created_at=ts,
                updated_at=ts,
            )
        )
        s.add(
            Marker(
                id=11,
                user_id=1,
                timeline="notifications",
                last_read_id=99,
                lock_version=1,
                created_at=ts,
                updated_at=ts,
            )
        )
        await s.commit()

    response = await client.get(
        "/api/v1/markers?timeline[]=home", headers=_AUTH
    )
    body = response.json()
    assert set(body) == {"home"}
    assert body["home"]["last_read_id"] == "12345"
    assert body["home"]["version"] == 3


@pytest.mark.asyncio
async def test_markers_post_upserts_and_bumps_version(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed_user(session_factory, seed_data)
    first = await client.post(
        "/api/v1/markers",
        json={"home": {"last_read_id": "1000"}},
        headers=_AUTH,
    )
    assert first.status_code == 200
    assert first.json()["home"]["last_read_id"] == "1000"
    assert first.json()["home"]["version"] == 0

    second = await client.post(
        "/api/v1/markers",
        json={"home": {"last_read_id": "2000"}},
        headers=_AUTH,
    )
    assert second.json()["home"]["last_read_id"] == "2000"
    assert second.json()["home"]["version"] == 1

    async with session_factory() as s:
        rows = (await s.execute(select(Marker))).scalars().all()
        assert len(rows) == 1
        assert rows[0].last_read_id == 2000


@pytest.mark.asyncio
async def test_markers_requires_auth(client: AsyncClient) -> None:
    assert (await client.get("/api/v1/markers?timeline[]=home")).status_code == 401


# ---------- preferences ----------


@pytest.mark.asyncio
async def test_preferences_returns_defaults(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed_user(session_factory, seed_data)
    response = await client.get("/api/v1/preferences", headers=_AUTH)
    assert response.status_code == 200
    body = response.json()
    assert body["posting:default:visibility"] == "public"
    assert body["posting:default:sensitive"] is False
    assert "reading:expand:spoilers" in body


@pytest.mark.asyncio
async def test_preferences_language_falls_back_to_user_locale(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    async with session_factory() as s:
        s.add_all(
            [
                seed_data["make_account"](id_=1, username="alice"),
                seed_data["make_account_stat"](account_id=1),
                seed_data["make_user"](id_=1, account_id=1, locale="fr"),
                seed_data["make_application"](),
                seed_data["make_token"](),
            ]
        )
        await s.commit()

    response = await client.get("/api/v1/preferences", headers=_AUTH)
    assert response.json()["posting:default:language"] == "fr"
