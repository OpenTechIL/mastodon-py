"""End-to-end tests for `/inbox` and `/users/{username}/inbox`.

These hit the real FastAPI app via the `client` fixture, signing
the POST body with a freshly-minted RSA key and seeding the remote
actor into the DB so verification finds the public key without a
network roundtrip.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import json

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import respx

from app.python.federation.activity import clear_activity_dedup_cache
from app.python.federation.signatures import sign_request
from app.python.models import Account, Block, Favourite, Follow, FollowRequest, Status, Visibility


@pytest.fixture(autouse=True)
def reset_activity_dedup() -> None:
    """Clear the in-process activity-ID dedup cache between tests.

    The cache is intentionally module-global (process-level) in production,
    but tests reuse the same activity IDs across test functions — without
    clearing, the second test that POSTs the same `id` would be a no-op.
    """
    clear_activity_dedup_cache()


def _make_keypair() -> tuple[bytes, bytes]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return priv, pub


@pytest.fixture(scope="module")
def keypair() -> tuple[bytes, bytes]:
    return _make_keypair()


async def _seed_remote_alice(
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
    *,
    public_key: bytes,
) -> str:
    """Seed a remote `alice@example.test` actor in the DB so the
    signature resolver finds the public key locally."""
    actor_url = "https://example.test/users/alice"
    async with session_factory() as s:
        s.add(
            seed_data["make_account"](
                id_=500,
                username="alice",
                domain="example.test",
                uri=actor_url,
                public_key=public_key.decode("utf-8"),
            )
        )
        s.add(seed_data["make_account_stat"](account_id=500))
        await s.commit()
    return actor_url


async def _seed_local_bob(
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    """Seed a *local* bob (no domain) — the per-actor inbox target."""
    async with session_factory() as s:
        s.add(seed_data["make_account"](id_=600, username="bob", domain=None))
        s.add(seed_data["make_account_stat"](account_id=600))
        await s.commit()


def _signed_headers(
    priv: bytes, *, actor_url: str, host: str, path: str, body: bytes
) -> dict[str, str]:
    headers: dict[str, str] = {
        "Host": host,
        "Content-Type": "application/activity+json",
    }
    sign_request(
        method="POST",
        path=path,
        headers=headers,
        body=body,
        key_id=f"{actor_url}#main-key",
        private_key_pem=priv,
        # Use current time so requests pass the ±12h date-skew check.
    )
    return headers


@pytest.mark.asyncio
async def test_shared_inbox_rejects_unsigned(client: AsyncClient) -> None:
    response = await client.post(
        "/inbox",
        content=b'{"type":"Create"}',
        headers={"Content-Type": "application/activity+json"},
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_shared_inbox_accepts_valid_signature(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
    keypair: tuple[bytes, bytes],
) -> None:
    priv, pub = keypair
    actor_url = await _seed_remote_alice(
        session_factory, seed_data, public_key=pub
    )
    body = b'{"@context":"https://www.w3.org/ns/activitystreams","type":"Create"}'
    headers = _signed_headers(
        priv, actor_url=actor_url, host="test", path="/inbox", body=body
    )

    response = await client.post("/inbox", content=body, headers=headers)
    assert response.status_code == 202, response.text


@pytest.mark.asyncio
async def test_shared_inbox_rejects_tampered_body(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
    keypair: tuple[bytes, bytes],
) -> None:
    priv, pub = keypair
    actor_url = await _seed_remote_alice(
        session_factory, seed_data, public_key=pub
    )
    body = b'{"type":"Create"}'
    headers = _signed_headers(
        priv, actor_url=actor_url, host="test", path="/inbox", body=body
    )
    # Submit a different body than the one we signed/digested.
    response = await client.post(
        "/inbox", content=b'{"type":"Delete"}', headers=headers
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_actor_inbox_404_for_unknown_user(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
    keypair: tuple[bytes, bytes],
) -> None:
    """Even with a valid signature, an inbox URL for a user we don't
    host returns 404 — we don't accept POSTs to fictional paths."""
    priv, pub = keypair
    actor_url = await _seed_remote_alice(
        session_factory, seed_data, public_key=pub
    )
    body = b'{"type":"Like"}'
    headers = _signed_headers(
        priv,
        actor_url=actor_url,
        host="test",
        path="/users/ghost/inbox",
        body=body,
    )
    response = await client.post(
        "/users/ghost/inbox", content=body, headers=headers
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_actor_inbox_accepts_for_local_actor(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
    keypair: tuple[bytes, bytes],
) -> None:
    priv, pub = keypair
    actor_url = await _seed_remote_alice(
        session_factory, seed_data, public_key=pub
    )
    await _seed_local_bob(session_factory, seed_data)
    body = b'{"type":"Follow"}'
    headers = _signed_headers(
        priv,
        actor_url=actor_url,
        host="test",
        path="/users/bob/inbox",
        body=body,
    )
    response = await client.post(
        "/users/bob/inbox", content=body, headers=headers
    )
    assert response.status_code == 202


@pytest.mark.asyncio
async def test_actor_inbox_does_not_route_to_remote_lookalike(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
    keypair: tuple[bytes, bytes],
) -> None:
    """A remote account with username `alice` also exists — but the
    inbox lookup must require `domain IS NULL` so we don't accept
    POSTs targeted at a remote user we happen to know about."""
    priv, pub = keypair
    await _seed_remote_alice(session_factory, seed_data, public_key=pub)
    body = b'{"type":"Create"}'
    headers = _signed_headers(
        priv,
        actor_url="https://example.test/users/alice",
        host="test",
        path="/users/alice/inbox",
        body=body,
    )
    response = await client.post(
        "/users/alice/inbox", content=body, headers=headers
    )
    assert response.status_code == 404


# ---------- Activity dispatch ----------


async def _seed_local_bob(
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
    *,
    locked: bool = False,
) -> int:
    """Seed local bob (the Follow target). Returns the account_id."""
    async with session_factory() as s:
        s.add(
            seed_data["make_account"](
                id_=600,
                username="bob",
                domain=None,
                locked=locked,
            )
        )
        s.add(seed_data["make_account_stat"](account_id=600))
        await s.commit()
    return 600


@pytest.mark.asyncio
async def test_inbox_follow_creates_follow_row(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
    keypair: tuple[bytes, bytes],
) -> None:
    priv, pub = keypair
    actor_url = await _seed_remote_alice(
        session_factory, seed_data, public_key=pub
    )
    await _seed_local_bob(session_factory, seed_data)

    body = json.dumps({
        "@context": "https://www.w3.org/ns/activitystreams",
        "id": "https://example.test/users/alice#follow/1",
        "type": "Follow",
        "actor": actor_url,
        "object": "http://test/users/bob",
    }).encode("utf-8")
    headers = _signed_headers(
        priv, actor_url=actor_url, host="test",
        path="/users/bob/inbox", body=body,
    )
    response = await client.post(
        "/users/bob/inbox", content=body, headers=headers
    )
    assert response.status_code == 202

    async with session_factory() as s:
        rows = (await s.execute(select(Follow))).scalars().all()
        assert len(rows) == 1
        assert rows[0].account_id == 500  # remote alice
        assert rows[0].target_account_id == 600  # local bob
        assert rows[0].uri == "https://example.test/users/alice#follow/1"


@pytest.mark.asyncio
async def test_inbox_follow_to_locked_account_creates_follow_request(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
    keypair: tuple[bytes, bytes],
) -> None:
    priv, pub = keypair
    actor_url = await _seed_remote_alice(
        session_factory, seed_data, public_key=pub
    )
    await _seed_local_bob(session_factory, seed_data, locked=True)

    body = json.dumps({
        "type": "Follow",
        "actor": actor_url,
        "object": "http://test/users/bob",
    }).encode("utf-8")
    headers = _signed_headers(
        priv, actor_url=actor_url, host="test",
        path="/users/bob/inbox", body=body,
    )
    response = await client.post(
        "/users/bob/inbox", content=body, headers=headers
    )
    assert response.status_code == 202

    async with session_factory() as s:
        # No Follow yet — that's the locked-account contract.
        follows = (await s.execute(select(Follow))).scalars().all()
        assert follows == []
        # FollowRequest pending instead.
        reqs = (await s.execute(select(FollowRequest))).scalars().all()
        assert len(reqs) == 1
        assert reqs[0].account_id == 500
        assert reqs[0].target_account_id == 600


@pytest.mark.asyncio
async def test_inbox_follow_is_idempotent(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
    keypair: tuple[bytes, bytes],
) -> None:
    priv, pub = keypair
    actor_url = await _seed_remote_alice(
        session_factory, seed_data, public_key=pub
    )
    await _seed_local_bob(session_factory, seed_data)

    body = json.dumps({
        "id": "https://example.test/users/alice#follow/1",
        "type": "Follow",
        "actor": actor_url,
        "object": "http://test/users/bob",
    }).encode("utf-8")
    headers = _signed_headers(
        priv, actor_url=actor_url, host="test",
        path="/users/bob/inbox", body=body,
    )
    await client.post("/users/bob/inbox", content=body, headers=headers)
    await client.post("/users/bob/inbox", content=body, headers=headers)

    async with session_factory() as s:
        rows = (await s.execute(select(Follow))).scalars().all()
        assert len(rows) == 1


@pytest.mark.asyncio
async def test_inbox_undo_follow_deletes_follow_row(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
    keypair: tuple[bytes, bytes],
) -> None:
    priv, pub = keypair
    actor_url = await _seed_remote_alice(
        session_factory, seed_data, public_key=pub
    )
    await _seed_local_bob(session_factory, seed_data)

    # First the Follow.
    follow_body = json.dumps({
        "id": "https://example.test/users/alice#follow/1",
        "type": "Follow",
        "actor": actor_url,
        "object": "http://test/users/bob",
    }).encode("utf-8")
    await client.post(
        "/users/bob/inbox",
        content=follow_body,
        headers=_signed_headers(
            priv, actor_url=actor_url, host="test",
            path="/users/bob/inbox", body=follow_body,
        ),
    )

    # Then the Undo, with the Follow nested inline.
    undo_body = json.dumps({
        "type": "Undo",
        "actor": actor_url,
        "object": {
            "id": "https://example.test/users/alice#follow/1",
            "type": "Follow",
            "actor": actor_url,
            "object": "http://test/users/bob",
        },
    }).encode("utf-8")
    response = await client.post(
        "/users/bob/inbox",
        content=undo_body,
        headers=_signed_headers(
            priv, actor_url=actor_url, host="test",
            path="/users/bob/inbox", body=undo_body,
        ),
    )
    assert response.status_code == 202

    async with session_factory() as s:
        rows = (await s.execute(select(Follow))).scalars().all()
        assert rows == []


@pytest.mark.asyncio
async def test_inbox_undo_follow_by_uri_also_works(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
    keypair: tuple[bytes, bytes],
) -> None:
    """Some peers Undo a Follow by just citing the Follow's id (string),
    not nesting the whole object."""
    priv, pub = keypair
    actor_url = await _seed_remote_alice(
        session_factory, seed_data, public_key=pub
    )
    await _seed_local_bob(session_factory, seed_data)

    follow_uri = "https://example.test/users/alice#follow/1"
    follow_body = json.dumps({
        "id": follow_uri,
        "type": "Follow",
        "actor": actor_url,
        "object": "http://test/users/bob",
    }).encode("utf-8")
    await client.post(
        "/users/bob/inbox",
        content=follow_body,
        headers=_signed_headers(
            priv, actor_url=actor_url, host="test",
            path="/users/bob/inbox", body=follow_body,
        ),
    )

    undo_body = json.dumps({
        "type": "Undo",
        "actor": actor_url,
        "object": follow_uri,
    }).encode("utf-8")
    response = await client.post(
        "/users/bob/inbox",
        content=undo_body,
        headers=_signed_headers(
            priv, actor_url=actor_url, host="test",
            path="/users/bob/inbox", body=undo_body,
        ),
    )
    assert response.status_code == 202

    async with session_factory() as s:
        rows = (await s.execute(select(Follow))).scalars().all()
        assert rows == []


@pytest.mark.asyncio
async def test_inbox_follow_with_non_local_target_is_noop(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
    keypair: tuple[bytes, bytes],
) -> None:
    """`object` points at a remote URL we don't host — accept the
    activity (202) but don't create a Follow row out of thin air."""
    priv, pub = keypair
    actor_url = await _seed_remote_alice(
        session_factory, seed_data, public_key=pub
    )
    body = json.dumps({
        "type": "Follow",
        "actor": actor_url,
        "object": "https://elsewhere.test/users/somebody",
    }).encode("utf-8")
    response = await client.post(
        "/inbox",
        content=body,
        headers=_signed_headers(
            priv, actor_url=actor_url, host="test",
            path="/inbox", body=body,
        ),
    )
    assert response.status_code == 202

    async with session_factory() as s:
        rows = (await s.execute(select(Follow))).scalars().all()
        assert rows == []


# ---------- Create / Delete ----------


def _create_note_body(*, note_id: str, actor_url: str, **extra: Any) -> bytes:
    note: dict[str, Any] = {
        "id": note_id,
        "type": "Note",
        "attributedTo": actor_url,
        "content": "<p>hello world</p>",
        "to": ["https://www.w3.org/ns/activitystreams#Public"],
        "cc": [],
    }
    note.update(extra)
    return json.dumps({
        "type": "Create",
        "actor": actor_url,
        "object": note,
    }).encode("utf-8")


@pytest.mark.asyncio
async def test_inbox_create_note_inserts_status(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
    keypair: tuple[bytes, bytes],
) -> None:
    priv, pub = keypair
    actor_url = await _seed_remote_alice(
        session_factory, seed_data, public_key=pub
    )
    body = _create_note_body(
        note_id="https://example.test/users/alice/statuses/1",
        actor_url=actor_url,
    )
    headers = _signed_headers(
        priv, actor_url=actor_url, host="test", path="/inbox", body=body
    )
    response = await client.post("/inbox", content=body, headers=headers)
    assert response.status_code == 202

    async with session_factory() as s:
        rows = (await s.execute(select(Status))).scalars().all()
        assert len(rows) == 1
        assert rows[0].uri == "https://example.test/users/alice/statuses/1"
        assert rows[0].text == "<p>hello world</p>"
        assert rows[0].account_id == 500
        assert rows[0].local is False
        assert rows[0].visibility == Visibility.PUBLIC.value


@pytest.mark.asyncio
async def test_inbox_create_note_is_idempotent(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
    keypair: tuple[bytes, bytes],
) -> None:
    priv, pub = keypair
    actor_url = await _seed_remote_alice(
        session_factory, seed_data, public_key=pub
    )
    body = _create_note_body(
        note_id="https://example.test/users/alice/statuses/dup",
        actor_url=actor_url,
    )
    headers = _signed_headers(
        priv, actor_url=actor_url, host="test", path="/inbox", body=body
    )
    await client.post("/inbox", content=body, headers=headers)
    await client.post("/inbox", content=body, headers=headers)

    async with session_factory() as s:
        rows = (await s.execute(select(Status))).scalars().all()
        assert len(rows) == 1


@pytest.mark.asyncio
async def test_inbox_create_note_unlisted_visibility(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
    keypair: tuple[bytes, bytes],
) -> None:
    """Public in `cc`, not in `to` → unlisted."""
    priv, pub = keypair
    actor_url = await _seed_remote_alice(
        session_factory, seed_data, public_key=pub
    )
    body = _create_note_body(
        note_id="https://example.test/users/alice/statuses/u",
        actor_url=actor_url,
        to=[f"{actor_url}/followers"],
        cc=["https://www.w3.org/ns/activitystreams#Public"],
    )
    await client.post(
        "/inbox",
        content=body,
        headers=_signed_headers(
            priv, actor_url=actor_url, host="test", path="/inbox", body=body
        ),
    )
    async with session_factory() as s:
        rows = (await s.execute(select(Status))).scalars().all()
        assert rows[0].visibility == Visibility.UNLISTED.value


@pytest.mark.asyncio
async def test_inbox_create_note_followers_only_visibility(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
    keypair: tuple[bytes, bytes],
) -> None:
    """Followers URL in `to`, no Public → private."""
    priv, pub = keypair
    actor_url = await _seed_remote_alice(
        session_factory, seed_data, public_key=pub
    )
    body = _create_note_body(
        note_id="https://example.test/users/alice/statuses/f",
        actor_url=actor_url,
        to=[f"{actor_url}/followers"],
        cc=[],
    )
    await client.post(
        "/inbox",
        content=body,
        headers=_signed_headers(
            priv, actor_url=actor_url, host="test", path="/inbox", body=body
        ),
    )
    async with session_factory() as s:
        rows = (await s.execute(select(Status))).scalars().all()
        assert rows[0].visibility == Visibility.PRIVATE.value


@pytest.mark.asyncio
async def test_inbox_create_note_direct_visibility(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
    keypair: tuple[bytes, bytes],
) -> None:
    """No Public anywhere, no followers URL → direct."""
    priv, pub = keypair
    actor_url = await _seed_remote_alice(
        session_factory, seed_data, public_key=pub
    )
    body = _create_note_body(
        note_id="https://example.test/users/alice/statuses/d",
        actor_url=actor_url,
        to=["http://test/users/bob"],
        cc=[],
    )
    await client.post(
        "/inbox",
        content=body,
        headers=_signed_headers(
            priv, actor_url=actor_url, host="test", path="/inbox", body=body
        ),
    )
    async with session_factory() as s:
        rows = (await s.execute(select(Status))).scalars().all()
        assert rows[0].visibility == Visibility.DIRECT.value


@pytest.mark.asyncio
async def test_inbox_create_note_attributedTo_mismatch_dropped(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
    keypair: tuple[bytes, bytes],
) -> None:
    """`attributedTo` disagreeing with the verified actor must not
    persist — we'd be letting alice's signature post as bob."""
    priv, pub = keypair
    actor_url = await _seed_remote_alice(
        session_factory, seed_data, public_key=pub
    )
    body = _create_note_body(
        note_id="https://example.test/users/alice/statuses/spoof",
        actor_url=actor_url,
        attributedTo="https://other.test/users/bob",  # mismatch
    )
    response = await client.post(
        "/inbox",
        content=body,
        headers=_signed_headers(
            priv, actor_url=actor_url, host="test", path="/inbox", body=body
        ),
    )
    assert response.status_code == 202  # accepted-and-dropped, not error
    async with session_factory() as s:
        rows = (await s.execute(select(Status))).scalars().all()
        assert rows == []


@pytest.mark.asyncio
async def test_inbox_create_note_threads_reply_when_parent_known(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
    keypair: tuple[bytes, bytes],
) -> None:
    """`inReplyTo` pointing at a Status we already have → set
    in_reply_to_id + reply=True. Unknown parents are stored as
    standalone toots."""
    from datetime import datetime as _dt

    priv, pub = keypair
    actor_url = await _seed_remote_alice(
        session_factory, seed_data, public_key=pub
    )
    # Seed a parent toot the reply will thread under.
    parent_uri = "http://test/users/bob/statuses/parent"
    async with session_factory() as s:
        s.add(seed_data["make_account"](id_=700, username="bob"))
        s.add(seed_data["make_account_stat"](account_id=700))
        s.add(
            seed_data["make_status"](
                id_=42, account_id=700, uri=parent_uri
            )
        )
        await s.commit()

    body = _create_note_body(
        note_id="https://example.test/users/alice/statuses/reply",
        actor_url=actor_url,
        inReplyTo=parent_uri,
    )
    await client.post(
        "/inbox",
        content=body,
        headers=_signed_headers(
            priv, actor_url=actor_url, host="test", path="/inbox", body=body
        ),
    )

    async with session_factory() as s:
        rows = (
            await s.execute(select(Status).where(Status.account_id == 500))
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].in_reply_to_id == 42
        assert rows[0].in_reply_to_account_id == 700
        assert rows[0].reply is True


@pytest.mark.asyncio
async def test_inbox_delete_soft_deletes_status(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
    keypair: tuple[bytes, bytes],
) -> None:
    priv, pub = keypair
    actor_url = await _seed_remote_alice(
        session_factory, seed_data, public_key=pub
    )
    note_id = "https://example.test/users/alice/statuses/will-die"

    # Create then delete.
    create_body = _create_note_body(note_id=note_id, actor_url=actor_url)
    await client.post(
        "/inbox",
        content=create_body,
        headers=_signed_headers(
            priv, actor_url=actor_url, host="test", path="/inbox", body=create_body
        ),
    )
    delete_body = json.dumps({
        "type": "Delete",
        "actor": actor_url,
        "object": {"id": note_id, "type": "Tombstone"},
    }).encode("utf-8")
    response = await client.post(
        "/inbox",
        content=delete_body,
        headers=_signed_headers(
            priv, actor_url=actor_url, host="test", path="/inbox", body=delete_body
        ),
    )
    assert response.status_code == 202

    async with session_factory() as s:
        row = (
            await s.execute(select(Status).where(Status.uri == note_id))
        ).scalar_one()
        assert row.deleted_at is not None


@pytest.mark.asyncio
async def test_inbox_delete_cannot_delete_other_actors_status(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
    keypair: tuple[bytes, bytes],
) -> None:
    """Alice signs a Delete for bob's status — must not delete it."""
    priv, pub = keypair
    actor_url = await _seed_remote_alice(
        session_factory, seed_data, public_key=pub
    )
    # Seed bob and one of his statuses.
    async with session_factory() as s:
        s.add(seed_data["make_account"](id_=701, username="bob"))
        s.add(seed_data["make_account_stat"](account_id=701))
        s.add(
            seed_data["make_status"](
                id_=99, account_id=701, uri="http://other.test/users/bob/statuses/99"
            )
        )
        await s.commit()

    body = json.dumps({
        "type": "Delete",
        "actor": actor_url,
        "object": "http://other.test/users/bob/statuses/99",
    }).encode("utf-8")
    await client.post(
        "/inbox",
        content=body,
        headers=_signed_headers(
            priv, actor_url=actor_url, host="test", path="/inbox", body=body
        ),
    )
    async with session_factory() as s:
        row = (
            await s.execute(select(Status).where(Status.id == 99))
        ).scalar_one()
        assert row.deleted_at is None


# ---------- Like / Announce ----------


async def _seed_local_status(
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
    *,
    status_id: int = 800,
    author_id: int = 700,
    uri: str = "http://test/users/bob/statuses/1",
) -> str:
    """Seed a local author (bob) + one of their statuses + status_stat."""
    async with session_factory() as s:
        s.add(seed_data["make_account"](id_=author_id, username="bob"))
        s.add(seed_data["make_account_stat"](account_id=author_id))
        s.add(
            seed_data["make_status"](
                id_=status_id, account_id=author_id, uri=uri
            )
        )
        if "make_status_stat" in seed_data:
            s.add(seed_data["make_status_stat"](status_id=status_id))
        await s.commit()
    return uri


@pytest.mark.asyncio
async def test_inbox_like_creates_favourite(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
    keypair: tuple[bytes, bytes],
) -> None:
    priv, pub = keypair
    actor_url = await _seed_remote_alice(
        session_factory, seed_data, public_key=pub
    )
    target_uri = await _seed_local_status(session_factory, seed_data)

    body = json.dumps({
        "id": f"{actor_url}#likes/1",
        "type": "Like",
        "actor": actor_url,
        "object": target_uri,
    }).encode("utf-8")
    response = await client.post(
        "/inbox",
        content=body,
        headers=_signed_headers(
            priv, actor_url=actor_url, host="test", path="/inbox", body=body
        ),
    )
    assert response.status_code == 202

    async with session_factory() as s:
        rows = (await s.execute(select(Favourite))).scalars().all()
        assert len(rows) == 1
        assert rows[0].account_id == 500  # remote alice
        assert rows[0].status_id == 800


@pytest.mark.asyncio
async def test_inbox_like_is_idempotent(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
    keypair: tuple[bytes, bytes],
) -> None:
    priv, pub = keypair
    actor_url = await _seed_remote_alice(
        session_factory, seed_data, public_key=pub
    )
    target_uri = await _seed_local_status(session_factory, seed_data)

    body = json.dumps({
        "type": "Like", "actor": actor_url, "object": target_uri,
    }).encode("utf-8")
    headers = _signed_headers(
        priv, actor_url=actor_url, host="test", path="/inbox", body=body
    )
    await client.post("/inbox", content=body, headers=headers)
    await client.post("/inbox", content=body, headers=headers)

    async with session_factory() as s:
        rows = (await s.execute(select(Favourite))).scalars().all()
        assert len(rows) == 1


@pytest.mark.asyncio
async def test_inbox_undo_like_deletes_favourite(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
    keypair: tuple[bytes, bytes],
) -> None:
    priv, pub = keypair
    actor_url = await _seed_remote_alice(
        session_factory, seed_data, public_key=pub
    )
    target_uri = await _seed_local_status(session_factory, seed_data)

    like_body = json.dumps({
        "id": f"{actor_url}#likes/1",
        "type": "Like", "actor": actor_url, "object": target_uri,
    }).encode("utf-8")
    await client.post(
        "/inbox",
        content=like_body,
        headers=_signed_headers(
            priv, actor_url=actor_url, host="test", path="/inbox", body=like_body
        ),
    )

    undo_body = json.dumps({
        "type": "Undo",
        "actor": actor_url,
        "object": {
            "id": f"{actor_url}#likes/1",
            "type": "Like",
            "actor": actor_url,
            "object": target_uri,
        },
    }).encode("utf-8")
    response = await client.post(
        "/inbox",
        content=undo_body,
        headers=_signed_headers(
            priv, actor_url=actor_url, host="test", path="/inbox", body=undo_body
        ),
    )
    assert response.status_code == 202

    async with session_factory() as s:
        rows = (await s.execute(select(Favourite))).scalars().all()
        assert rows == []


@pytest.mark.asyncio
async def test_inbox_announce_creates_reblog_status(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
    keypair: tuple[bytes, bytes],
) -> None:
    priv, pub = keypair
    actor_url = await _seed_remote_alice(
        session_factory, seed_data, public_key=pub
    )
    target_uri = await _seed_local_status(session_factory, seed_data)
    announce_uri = "https://example.test/users/alice/statuses/boost/1"

    body = json.dumps({
        "id": announce_uri,
        "type": "Announce",
        "actor": actor_url,
        "object": target_uri,
        "to": ["https://www.w3.org/ns/activitystreams#Public"],
    }).encode("utf-8")
    response = await client.post(
        "/inbox",
        content=body,
        headers=_signed_headers(
            priv, actor_url=actor_url, host="test", path="/inbox", body=body
        ),
    )
    assert response.status_code == 202

    async with session_factory() as s:
        # The reblog Status row: account is alice, reblog_of_id points at target.
        rows = (
            await s.execute(
                select(Status).where(
                    Status.account_id == 500,
                    Status.reblog_of_id == 800,
                )
            )
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].uri == announce_uri
        assert rows[0].local is False
        assert rows[0].visibility == Visibility.PUBLIC.value


@pytest.mark.asyncio
async def test_inbox_announce_is_idempotent(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
    keypair: tuple[bytes, bytes],
) -> None:
    priv, pub = keypair
    actor_url = await _seed_remote_alice(
        session_factory, seed_data, public_key=pub
    )
    target_uri = await _seed_local_status(session_factory, seed_data)
    body = json.dumps({
        "id": "https://example.test/boost/1",
        "type": "Announce", "actor": actor_url, "object": target_uri,
    }).encode("utf-8")
    headers = _signed_headers(
        priv, actor_url=actor_url, host="test", path="/inbox", body=body
    )
    await client.post("/inbox", content=body, headers=headers)
    await client.post("/inbox", content=body, headers=headers)

    async with session_factory() as s:
        rows = (
            await s.execute(
                select(Status).where(Status.reblog_of_id == 800)
            )
        ).scalars().all()
        assert len(rows) == 1


@pytest.mark.asyncio
async def test_inbox_undo_announce_soft_deletes_reblog(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
    keypair: tuple[bytes, bytes],
) -> None:
    priv, pub = keypair
    actor_url = await _seed_remote_alice(
        session_factory, seed_data, public_key=pub
    )
    target_uri = await _seed_local_status(session_factory, seed_data)
    announce_uri = "https://example.test/boost/1"

    boost_body = json.dumps({
        "id": announce_uri,
        "type": "Announce", "actor": actor_url, "object": target_uri,
    }).encode("utf-8")
    await client.post(
        "/inbox",
        content=boost_body,
        headers=_signed_headers(
            priv, actor_url=actor_url, host="test", path="/inbox", body=boost_body
        ),
    )

    undo_body = json.dumps({
        "type": "Undo",
        "actor": actor_url,
        "object": {
            "id": announce_uri,
            "type": "Announce",
            "actor": actor_url,
            "object": target_uri,
        },
    }).encode("utf-8")
    await client.post(
        "/inbox",
        content=undo_body,
        headers=_signed_headers(
            priv, actor_url=actor_url, host="test", path="/inbox", body=undo_body
        ),
    )

    async with session_factory() as s:
        rows = (
            await s.execute(
                select(Status).where(Status.reblog_of_id == 800)
            )
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].deleted_at is not None


@pytest.mark.asyncio
async def test_inbox_like_for_unknown_status_is_noop(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
    keypair: tuple[bytes, bytes],
) -> None:
    """Like targeting a Status we don't have → 202, no Favourite row."""
    priv, pub = keypair
    actor_url = await _seed_remote_alice(
        session_factory, seed_data, public_key=pub
    )
    body = json.dumps({
        "type": "Like",
        "actor": actor_url,
        "object": "http://elsewhere.test/users/x/statuses/missing",
    }).encode("utf-8")
    response = await client.post(
        "/inbox",
        content=body,
        headers=_signed_headers(
            priv, actor_url=actor_url, host="test", path="/inbox", body=body
        ),
    )
    assert response.status_code == 202
    async with session_factory() as s:
        rows = (await s.execute(select(Favourite))).scalars().all()
        assert rows == []


# ---------- First contact (auto actor ingestion) ----------


@pytest.mark.asyncio
async def test_inbox_follow_from_unknown_actor_ingests_then_creates_follow(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
    keypair: tuple[bytes, bytes],
) -> None:
    """First-contact path: a peer we've never seen sends a Follow.
    Dispatcher must fetch their actor JSON, create an Account stub,
    THEN run the Follow handler."""
    priv, pub = keypair
    actor_url = "https://newpeer.test/users/dave"
    # We have NOT pre-seeded dave. The signature verifier will need
    # the public key on first contact — it fetches it via the same
    # actor URL.
    await _seed_local_bob(session_factory, seed_data)

    actor_json = {
        "id": actor_url,
        "type": "Person",
        "preferredUsername": "dave",
        "inbox": f"{actor_url}/inbox",
        "publicKey": {
            "id": f"{actor_url}#main-key",
            "owner": actor_url,
            "publicKeyPem": pub.decode("utf-8"),
        },
    }
    body = json.dumps({
        "id": f"{actor_url}#follow/1",
        "type": "Follow",
        "actor": actor_url,
        "object": "http://test/users/bob",
    }).encode("utf-8")
    headers = _signed_headers(
        priv, actor_url=actor_url, host="test",
        path="/users/bob/inbox", body=body,
    )

    async with respx.mock() as router:
        router.get(actor_url).respond(json=actor_json)
        response = await client.post(
            "/users/bob/inbox", content=body, headers=headers
        )
    assert response.status_code == 202

    async with session_factory() as s:
        # Account stub got persisted.
        rows = (
            await s.execute(select(Account).where(Account.uri == actor_url))
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].domain == "newpeer.test"
        assert rows[0].username == "dave"
        # Follow row points at the stub.
        follows = (await s.execute(select(Follow))).scalars().all()
        assert len(follows) == 1
        assert follows[0].account_id == rows[0].id


# ---------- Outbound Accept on auto-accepted Follow ----------


@pytest.mark.asyncio
async def test_inbox_follow_to_unlocked_account_enqueues_accept(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
    keypair: tuple[bytes, bytes],
    fake_enqueuer,
) -> None:
    """Auto-accept: bob follows alice (unlocked) → we enqueue an Accept
    delivery to bob's inbox. The Accept's `object` wraps bob's
    original Follow so his client can correlate."""
    priv, pub = keypair
    # Seed remote alice/bob with shared+inbox URLs so collect_inbox_urls
    # picks them up. Here `alice` is the remote follower, `bob` is the
    # local target — naming flipped from the other tests.
    actor_url = await _seed_remote_alice(
        session_factory, seed_data, public_key=pub
    )
    # Override alice's inbox URL so the Accept knows where to land.
    async with session_factory() as s:
        from sqlalchemy import select as _select
        from app.python.models import Account
        row = (
            await s.execute(_select(Account).where(Account.uri == actor_url))
        ).scalar_one()
        row.inbox_url = "https://example.test/users/alice/inbox"
        await s.commit()
    await _seed_local_bob(session_factory, seed_data, locked=False)

    body = json.dumps({
        "id": f"{actor_url}#follow/42",
        "type": "Follow",
        "actor": actor_url,
        "object": "http://test/users/bob",
    }).encode("utf-8")
    response = await client.post(
        "/users/bob/inbox",
        content=body,
        headers=_signed_headers(
            priv, actor_url=actor_url, host="test",
            path="/users/bob/inbox", body=body,
        ),
    )
    assert response.status_code == 202

    # Follow row persisted.
    async with session_factory() as s:
        from sqlalchemy import select as _select
        follows = (await s.execute(_select(Follow))).scalars().all()
        assert len(follows) == 1

    # Accept enqueued: one `deliver_activity` call with an Accept body
    # going to alice's inbox.
    deliveries = [c for c in fake_enqueuer.calls if c[0] == "deliver_activity"]
    assert len(deliveries) == 1
    _name, args = deliveries[0]
    activity, sender_id, inbox_urls = args
    assert activity["type"] == "Accept"
    assert activity["object"]["type"] == "Follow"
    assert activity["object"]["id"] == f"{actor_url}#follow/42"
    assert activity["actor"].endswith("/users/bob")
    assert sender_id == 600  # local bob id from _seed_local_bob
    assert inbox_urls == ["https://example.test/users/alice/inbox"]


@pytest.mark.asyncio
async def test_inbox_follow_to_locked_account_does_not_enqueue_accept(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
    keypair: tuple[bytes, bytes],
    fake_enqueuer,
) -> None:
    """Locked target → FollowRequest is created but NO Accept is sent.
    The authorize endpoint emits Accept when the user clicks through.
    Pinning this so the auto-accept hook doesn't accidentally
    short-circuit the locked-account workflow."""
    priv, pub = keypair
    actor_url = await _seed_remote_alice(
        session_factory, seed_data, public_key=pub
    )
    await _seed_local_bob(session_factory, seed_data, locked=True)

    body = json.dumps({
        "id": f"{actor_url}#follow/1",
        "type": "Follow",
        "actor": actor_url,
        "object": "http://test/users/bob",
    }).encode("utf-8")
    await client.post(
        "/users/bob/inbox",
        content=body,
        headers=_signed_headers(
            priv, actor_url=actor_url, host="test",
            path="/users/bob/inbox", body=body,
        ),
    )

    deliveries = [c for c in fake_enqueuer.calls if c[0] == "deliver_activity"]
    assert deliveries == []


@pytest.mark.asyncio
async def test_repeat_follow_does_not_resend_accept(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
    keypair: tuple[bytes, bytes],
    fake_enqueuer,
) -> None:
    """Idempotency: a peer re-delivering the same Follow (e.g. after
    not seeing our 202) shouldn't generate a duplicate Accept."""
    priv, pub = keypair
    actor_url = await _seed_remote_alice(
        session_factory, seed_data, public_key=pub
    )
    async with session_factory() as s:
        from sqlalchemy import select as _select
        from app.python.models import Account
        row = (
            await s.execute(_select(Account).where(Account.uri == actor_url))
        ).scalar_one()
        row.inbox_url = "https://example.test/users/alice/inbox"
        await s.commit()
    await _seed_local_bob(session_factory, seed_data, locked=False)

    body = json.dumps({
        "id": f"{actor_url}#follow/dup",
        "type": "Follow",
        "actor": actor_url,
        "object": "http://test/users/bob",
    }).encode("utf-8")
    headers = _signed_headers(
        priv, actor_url=actor_url, host="test",
        path="/users/bob/inbox", body=body,
    )
    await client.post("/users/bob/inbox", content=body, headers=headers)
    await client.post("/users/bob/inbox", content=body, headers=headers)

    deliveries = [c for c in fake_enqueuer.calls if c[0] == "deliver_activity"]
    assert len(deliveries) == 1  # only the first Follow triggered Accept


# ---------- Update (actor) ----------


@pytest.mark.asyncio
async def test_inbox_update_actor_overwrites_cached_profile_fields(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
    keypair: tuple[bytes, bytes],
) -> None:
    priv, pub = keypair
    actor_url = await _seed_remote_alice(
        session_factory, seed_data, public_key=pub
    )

    body = json.dumps({
        "type": "Update",
        "actor": actor_url,
        "object": {
            "id": actor_url,
            "type": "Person",
            "preferredUsername": "alice",
            "name": "Alice (new bio)",
            "summary": "<p>fresh summary</p>",
            "manuallyApprovesFollowers": True,
            "indexable": False,
            "publicKey": {
                "id": f"{actor_url}#main-key",
                "owner": actor_url,
                "publicKeyPem": pub.decode("utf-8"),
            },
        },
    }).encode("utf-8")
    response = await client.post(
        "/inbox",
        content=body,
        headers=_signed_headers(
            priv, actor_url=actor_url, host="test", path="/inbox", body=body
        ),
    )
    assert response.status_code == 202

    async with session_factory() as s:
        row = (
            await s.execute(select(Account).where(Account.uri == actor_url))
        ).scalar_one()
        assert row.display_name == "Alice (new bio)"
        assert row.note == "<p>fresh summary</p>"
        assert row.locked is True
        assert row.indexable is False


@pytest.mark.asyncio
async def test_inbox_update_actor_rotates_public_key(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
    keypair: tuple[bytes, bytes],
) -> None:
    """Key rotation: peers occasionally roll their RSA keys. The new
    PEM lands on the cached row so future signature verifications
    continue to work."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    priv, pub = keypair
    actor_url = await _seed_remote_alice(
        session_factory, seed_data, public_key=pub
    )

    new_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    new_pub = new_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")

    body = json.dumps({
        "type": "Update",
        "actor": actor_url,
        "object": {
            "id": actor_url,
            "type": "Person",
            "preferredUsername": "alice",
            "publicKey": {
                "id": f"{actor_url}#main-key",
                "owner": actor_url,
                "publicKeyPem": new_pub,
            },
        },
    }).encode("utf-8")
    # Signed with the OLD key — current request still verifies.
    response = await client.post(
        "/inbox",
        content=body,
        headers=_signed_headers(
            priv, actor_url=actor_url, host="test", path="/inbox", body=body
        ),
    )
    assert response.status_code == 202

    async with session_factory() as s:
        row = (
            await s.execute(select(Account).where(Account.uri == actor_url))
        ).scalar_one()
        assert row.public_key == new_pub


@pytest.mark.asyncio
async def test_inbox_update_actor_rejects_id_mismatch(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
    keypair: tuple[bytes, bytes],
) -> None:
    """The `object.id` must match the verified actor URL. Alice can't
    update bob just because she signed the activity."""
    priv, pub = keypair
    alice_url = await _seed_remote_alice(
        session_factory, seed_data, public_key=pub
    )
    # Seed a separate bob (different URI).
    async with session_factory() as s:
        s.add(
            seed_data["make_account"](
                id_=501, username="bob", domain="other.test",
                uri="https://other.test/users/bob",
                display_name="Bob",
            )
        )
        s.add(seed_data["make_account_stat"](account_id=501))
        await s.commit()

    body = json.dumps({
        "type": "Update",
        "actor": alice_url,
        "object": {
            "id": "https://other.test/users/bob",  # wrong target
            "type": "Person",
            "name": "Bob (hijacked)",
        },
    }).encode("utf-8")
    response = await client.post(
        "/inbox",
        content=body,
        headers=_signed_headers(
            priv, actor_url=alice_url, host="test", path="/inbox", body=body
        ),
    )
    assert response.status_code == 202  # accepted-and-dropped

    async with session_factory() as s:
        bob = (
            await s.execute(
                select(Account).where(Account.uri == "https://other.test/users/bob")
            )
        ).scalar_one()
        assert bob.display_name == "Bob"  # unchanged


@pytest.mark.asyncio
async def test_inbox_update_actor_preserves_fields_not_in_payload(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
    keypair: tuple[bytes, bytes],
) -> None:
    """Peers don't always send the full Person on Update. Fields
    they omit must keep the cached value, not be cleared to defaults."""
    priv, pub = keypair
    actor_url = await _seed_remote_alice(
        session_factory, seed_data, public_key=pub
    )
    # Set a display_name first so we can verify it survives an Update
    # that omits `name`.
    async with session_factory() as s:
        row = (
            await s.execute(select(Account).where(Account.uri == actor_url))
        ).scalar_one()
        row.display_name = "Original Display"
        await s.commit()

    body = json.dumps({
        "type": "Update",
        "actor": actor_url,
        "object": {
            "id": actor_url,
            "type": "Person",
            "summary": "<p>just a bio change</p>",
            # name intentionally absent
        },
    }).encode("utf-8")
    await client.post(
        "/inbox",
        content=body,
        headers=_signed_headers(
            priv, actor_url=actor_url, host="test", path="/inbox", body=body
        ),
    )

    async with session_factory() as s:
        row = (
            await s.execute(select(Account).where(Account.uri == actor_url))
        ).scalar_one()
        # display_name preserved.
        assert row.display_name == "Original Display"
        # summary updated.
        assert row.note == "<p>just a bio change</p>"


@pytest.mark.asyncio
async def test_inbox_unrecognized_activity_type_is_noop(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
    keypair: tuple[bytes, bytes],
) -> None:
    """An activity type we haven't ported yet (e.g. Like) still gets a
    202 — accepting receipt without acting on it is the right answer
    until the corresponding handler ships."""
    priv, pub = keypair
    actor_url = await _seed_remote_alice(
        session_factory, seed_data, public_key=pub
    )
    body = json.dumps({
        "type": "Like",
        "actor": actor_url,
        "object": "https://example.test/users/bob/statuses/123",
    }).encode("utf-8")
    response = await client.post(
        "/inbox",
        content=body,
        headers=_signed_headers(
            priv, actor_url=actor_url, host="test",
            path="/inbox", body=body,
        ),
    )
    assert response.status_code == 202


# ---------- Update Note ----------


@pytest.mark.asyncio
async def test_inbox_update_note_edits_status(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
    keypair: tuple[bytes, bytes],
) -> None:
    """Update{Note} overwrites text and stamps edited_at."""
    priv, pub = keypair
    actor_url = await _seed_remote_alice(session_factory, seed_data, public_key=pub)
    note_uri = "https://example.test/statuses/upd1"

    # First: Create the status via a Create activity.
    create_body = json.dumps({
        "id": "https://example.test/activities/create-upd1",
        "type": "Create",
        "actor": actor_url,
        "object": {
            "id": note_uri,
            "type": "Note",
            "attributedTo": actor_url,
            "content": "<p>original</p>",
            "to": ["https://www.w3.org/ns/activitystreams#Public"],
            "cc": [],
        },
    }).encode("utf-8")
    await client.post(
        "/inbox", content=create_body,
        headers=_signed_headers(priv, actor_url=actor_url, host="test", path="/inbox", body=create_body),
    )

    # Then: Update the same note.
    update_body = json.dumps({
        "id": "https://example.test/activities/update-upd1",
        "type": "Update",
        "actor": actor_url,
        "object": {
            "id": note_uri,
            "type": "Note",
            "attributedTo": actor_url,
            "content": "<p>edited</p>",
            "to": ["https://www.w3.org/ns/activitystreams#Public"],
            "cc": [],
        },
    }).encode("utf-8")
    response = await client.post(
        "/inbox", content=update_body,
        headers=_signed_headers(priv, actor_url=actor_url, host="test", path="/inbox", body=update_body),
    )
    assert response.status_code == 202

    async with session_factory() as s:
        row = (await s.execute(select(Status).where(Status.uri == note_uri))).scalar_one()
    assert row.text == "<p>edited</p>"
    assert row.edited_at is not None


@pytest.mark.asyncio
async def test_inbox_update_note_rejects_wrong_actor(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
    keypair: tuple[bytes, bytes],
) -> None:
    """Update{Note} where attributedTo != signing actor is ignored."""
    priv, pub = keypair
    actor_url = await _seed_remote_alice(session_factory, seed_data, public_key=pub)
    note_uri = "https://example.test/statuses/upd2"

    create_body = json.dumps({
        "id": "https://example.test/activities/create-upd2",
        "type": "Create",
        "actor": actor_url,
        "object": {
            "id": note_uri, "type": "Note", "attributedTo": actor_url,
            "content": "<p>original</p>",
            "to": ["https://www.w3.org/ns/activitystreams#Public"], "cc": [],
        },
    }).encode("utf-8")
    await client.post(
        "/inbox", content=create_body,
        headers=_signed_headers(priv, actor_url=actor_url, host="test", path="/inbox", body=create_body),
    )

    update_body = json.dumps({
        "id": "https://example.test/activities/update-upd2-bad",
        "type": "Update",
        "actor": actor_url,
        "object": {
            "id": note_uri, "type": "Note",
            "attributedTo": "https://evil.test/users/impersonator",
            "content": "<p>hijacked</p>",
            "to": ["https://www.w3.org/ns/activitystreams#Public"], "cc": [],
        },
    }).encode("utf-8")
    await client.post(
        "/inbox", content=update_body,
        headers=_signed_headers(priv, actor_url=actor_url, host="test", path="/inbox", body=update_body),
    )

    async with session_factory() as s:
        row = (await s.execute(select(Status).where(Status.uri == note_uri))).scalar_one()
    assert row.text == "<p>original</p>"  # not changed
    assert row.edited_at is None


# ---------- Block ----------


@pytest.mark.asyncio
async def test_inbox_block_stores_block_and_tears_down_follows(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
    keypair: tuple[bytes, bytes],
) -> None:
    """Block from remote actor targets local user → Block row created,
    existing Follow/FollowRequest in both directions removed."""
    priv, pub = keypair
    actor_url = await _seed_remote_alice(session_factory, seed_data, public_key=pub)
    await _seed_local_bob(session_factory, seed_data)

    # Pre-seed a Follow: alice→bob (remote follows local)
    async with session_factory() as s:
        from datetime import datetime, timezone
        alice = (await s.execute(select(Account).where(Account.uri == actor_url))).scalar_one()
        bob = (await s.execute(select(Account).where(Account.username == "bob", Account.domain.is_(None)))).scalar_one()
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        s.add(Follow(id=9901, account_id=alice.id, target_account_id=bob.id, created_at=now, updated_at=now))
        await s.commit()

    block_body = json.dumps({
        "id": "https://example.test/blocks/1",
        "type": "Block",
        "actor": actor_url,
        "object": "http://test/users/bob",
    }).encode("utf-8")
    response = await client.post(
        "/inbox", content=block_body,
        headers=_signed_headers(priv, actor_url=actor_url, host="test", path="/inbox", body=block_body),
    )
    assert response.status_code == 202

    async with session_factory() as s:
        blocks = (await s.execute(select(Block))).scalars().all()
        follows = (await s.execute(select(Follow))).scalars().all()
    assert len(blocks) == 1
    assert len(follows) == 0  # torn down


@pytest.mark.asyncio
async def test_inbox_block_is_idempotent(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
    keypair: tuple[bytes, bytes],
) -> None:
    """Duplicate Block activities don't create duplicate Block rows."""
    priv, pub = keypair
    actor_url = await _seed_remote_alice(session_factory, seed_data, public_key=pub)
    await _seed_local_bob(session_factory, seed_data)

    block_body = json.dumps({
        "id": "https://example.test/blocks/2",
        "type": "Block",
        "actor": actor_url,
        "object": "http://test/users/bob",
    }).encode("utf-8")
    headers = _signed_headers(priv, actor_url=actor_url, host="test", path="/inbox", body=block_body)
    await client.post("/inbox", content=block_body, headers=headers)

    block_body2 = json.dumps({
        "id": "https://example.test/blocks/2-b",  # different activity id, same block
        "type": "Block",
        "actor": actor_url,
        "object": "http://test/users/bob",
    }).encode("utf-8")
    headers2 = _signed_headers(priv, actor_url=actor_url, host="test", path="/inbox", body=block_body2)
    await client.post("/inbox", content=block_body2, headers=headers2)

    async with session_factory() as s:
        blocks = (await s.execute(select(Block))).scalars().all()
    assert len(blocks) == 1
