"""Tests for PATCH /api/v1/accounts/update_credentials."""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


_AUTH = {"Authorization": "Bearer raw-token-abc"}


async def _seed(
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    async with session_factory() as s:
        s.add_all(
            [
                seed_data["make_account"](id_=1, username="alice", note=""),
                seed_data["make_account_stat"](account_id=1),
                seed_data["make_user"](id_=1, account_id=1),
                seed_data["make_application"](),
                seed_data["make_token"](),
            ]
        )
        await s.commit()


@pytest.mark.asyncio
async def test_update_credentials_requires_auth(client: AsyncClient) -> None:
    response = await client.patch(
        "/api/v1/accounts/update_credentials",
        json={"display_name": "x"},
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_update_basic_fields(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    response = await client.patch(
        "/api/v1/accounts/update_credentials",
        json={
            "display_name": "Alice in Wonderland",
            "note": "Just a curious cat.",
            "locked": True,
            "bot": True,
            "discoverable": False,
        },
        headers=_AUTH,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["display_name"] == "Alice in Wonderland"
    assert "Just a curious cat." in body["note"]
    assert body["locked"] is True
    assert body["bot"] is True
    assert body["discoverable"] is False

    # verify_credentials reflects the change.
    verify = await client.get(
        "/api/v1/accounts/verify_credentials", headers=_AUTH
    )
    assert verify.json()["display_name"] == "Alice in Wonderland"


@pytest.mark.asyncio
async def test_partial_update_leaves_unset_fields_alone(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    await client.patch(
        "/api/v1/accounts/update_credentials",
        json={"display_name": "First Name"},
        headers=_AUTH,
    )
    # Send only `note`; display_name from previous update should persist.
    await client.patch(
        "/api/v1/accounts/update_credentials",
        json={"note": "hello"},
        headers=_AUTH,
    )
    verify = await client.get(
        "/api/v1/accounts/verify_credentials", headers=_AUTH
    )
    assert verify.json()["display_name"] == "First Name"


@pytest.mark.asyncio
async def test_fields_attributes_normalized(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    # Six rows, two blank — should collapse to first four non-blank entries.
    response = await client.patch(
        "/api/v1/accounts/update_credentials",
        json={
            "fields_attributes": [
                {"name": "  Website  ", "value": " https://example.test "},
                {"name": "", "value": ""},
                {"name": "Pronouns", "value": "she/her"},
                {"name": "Loc", "value": "Earth"},
                {"name": "Five", "value": "5"},
                {"name": "  ", "value": "  "},
            ]
        },
        headers=_AUTH,
    )
    assert response.status_code == 200
    fields = response.json()["fields"]
    assert len(fields) == 4
    # Whitespace stripped.
    assert fields[0]["name"] == "Website"
    assert fields[0]["value"] == "https://example.test"
    assert [f["name"] for f in fields] == ["Website", "Pronouns", "Loc", "Five"]


@pytest.mark.asyncio
async def test_clearing_fields_with_empty_array(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    # First set some fields.
    await client.patch(
        "/api/v1/accounts/update_credentials",
        json={"fields_attributes": [{"name": "foo", "value": "bar"}]},
        headers=_AUTH,
    )
    # Now clear them with an empty array.
    response = await client.patch(
        "/api/v1/accounts/update_credentials",
        json={"fields_attributes": []},
        headers=_AUTH,
    )
    assert response.json()["fields"] == []


@pytest.mark.asyncio
async def test_bot_toggle_round_trips(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    set_bot = await client.patch(
        "/api/v1/accounts/update_credentials",
        json={"bot": True},
        headers=_AUTH,
    )
    assert set_bot.json()["bot"] is True
    unset_bot = await client.patch(
        "/api/v1/accounts/update_credentials",
        json={"bot": False},
        headers=_AUTH,
    )
    assert unset_bot.json()["bot"] is False
