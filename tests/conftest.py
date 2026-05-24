"""Shared pytest fixtures.

The auth-layer tests run against an in-memory SQLite database with the
four tables Phase 1 needs (`accounts`, `users`, `oauth_applications`,
`oauth_access_tokens`). Tests override the FastAPI `get_session`
dependency so requests hit this database rather than the production
Postgres.

Later phases that need Postgres-specific types (jsonb, inet, array, full
text search) will graduate the fixture to `pytest-postgresql`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime

import bcrypt
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.python.db import Base, get_session
from app.python.main import create_app
from app.python.models import (
    Account,
    AccountStat,
    OAuthAccessToken,
    OAuthApplication,
    Status,
    StatusStat,
    User,
    Visibility,
)


def _hash_password(plaintext: str) -> str:
    return bcrypt.hashpw(plaintext.encode("utf-8"), bcrypt.gensalt(rounds=4)).decode("utf-8")


@pytest.fixture
def fixed_now() -> datetime:
    return datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def naive_now(fixed_now: datetime) -> datetime:
    return fixed_now.replace(tzinfo=None)


@pytest_asyncio.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def session(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with session_factory() as s:
        yield s


@pytest.fixture
def password_hash() -> str:
    return _hash_password("hunter2")


@pytest.fixture
def seed_data(naive_now: datetime, password_hash: str) -> dict[str, object]:
    """Plain-Python factory bundle for tests to insert what they need."""

    def make_account(
        *,
        id_: int = 1,
        username: str = "alice",
        domain: str | None = None,
        actor_type: str | None = None,
        suspended_at: datetime | None = None,
        note: str = "",
        **overrides: object,
    ) -> Account:
        defaults: dict[str, object] = {
            "id": id_,
            "username": username,
            "domain": domain,
            "display_name": username.capitalize(),
            "note": note,
            "actor_type": actor_type,
            "suspended_at": suspended_at,
            "uri": "",
            "header_remote_url": "",
            "created_at": naive_now,
            "updated_at": naive_now,
        }
        defaults.update(overrides)
        return Account(**defaults)  # type: ignore[arg-type]

    def make_account_stat(
        *,
        account_id: int = 1,
        statuses_count: int = 0,
        followers_count: int = 0,
        following_count: int = 0,
        last_status_at: datetime | None = None,
    ) -> AccountStat:
        return AccountStat(
            id=account_id,
            account_id=account_id,
            statuses_count=statuses_count,
            followers_count=followers_count,
            following_count=following_count,
            last_status_at=last_status_at,
            created_at=naive_now,
            updated_at=naive_now,
        )

    def make_status(
        *,
        id_: int,
        account_id: int = 1,
        text: str = "hello world",
        visibility: Visibility = Visibility.PUBLIC,
        reply: bool = False,
        in_reply_to_id: int | None = None,
        in_reply_to_account_id: int | None = None,
        reblog_of_id: int | None = None,
        local: bool | None = True,
        deleted_at: datetime | None = None,
        created_at: datetime | None = None,
        spoiler_text: str = "",
        sensitive: bool = False,
        language: str | None = "en",
        uri: str | None = None,
    ) -> Status:
        return Status(
            id=id_,
            account_id=account_id,
            text=text,
            spoiler_text=spoiler_text,
            sensitive=sensitive,
            visibility=visibility.value,
            language=language,
            local=local,
            reply=reply,
            in_reply_to_id=in_reply_to_id,
            in_reply_to_account_id=in_reply_to_account_id,
            reblog_of_id=reblog_of_id,
            uri=uri,
            url=None,
            edited_at=None,
            deleted_at=deleted_at,
            created_at=created_at or naive_now,
            updated_at=created_at or naive_now,
        )

    def make_status_stat(
        *,
        status_id: int,
        replies_count: int = 0,
        reblogs_count: int = 0,
        favourites_count: int = 0,
    ) -> StatusStat:
        return StatusStat(
            id=status_id,
            status_id=status_id,
            replies_count=replies_count,
            reblogs_count=reblogs_count,
            favourites_count=favourites_count,
            quotes_count=0,
            created_at=naive_now,
            updated_at=naive_now,
        )

    def make_user(*, id_: int = 1, account_id: int = 1, **overrides: object) -> User:
        defaults: dict[str, object] = {
            "id": id_,
            "account_id": account_id,
            "email": "alice@example.com",
            "encrypted_password": password_hash,
            "confirmed_at": naive_now,
            "approved": True,
            "disabled": False,
            "otp_required_for_login": False,
            "otp_secret": None,
            "consumed_timestep": None,
            "created_at": naive_now,
            "updated_at": naive_now,
        }
        defaults.update(overrides)
        return User(**defaults)  # type: ignore[arg-type]

    def make_application(*, id_: int = 1, scopes: str = "read write") -> OAuthApplication:
        return OAuthApplication(
            id=id_,
            name="Test Client",
            uid="uid-test",
            secret="secret-test",
            redirect_uri="urn:ietf:wg:oauth:2.0:oob",
            scopes=scopes,
            website="https://example.com",
            confidential=True,
            superapp=False,
            created_at=naive_now,
            updated_at=naive_now,
        )

    def make_token(
        *,
        id_: int = 1,
        application_id: int | None = 1,
        resource_owner_id: int | None = 1,
        token: str = "raw-token-abc",
        scopes: str | None = "read write",
        expires_in: int | None = None,
        revoked_at: datetime | None = None,
        created_at: datetime | None = None,
    ) -> OAuthAccessToken:
        return OAuthAccessToken(
            id=id_,
            token=token,
            refresh_token=None,
            scopes=scopes,
            application_id=application_id,
            resource_owner_id=resource_owner_id,
            expires_in=expires_in,
            revoked_at=revoked_at,
            created_at=created_at or naive_now,
            last_used_at=None,
            last_used_ip=None,
        )

    return {
        "make_account": make_account,
        "make_account_stat": make_account_stat,
        "make_user": make_user,
        "make_application": make_application,
        "make_token": make_token,
        "make_status": make_status,
        "make_status_stat": make_status_stat,
    }


class FakeEnqueuer:
    """Captures enqueue calls so tests can assert on them without Redis."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    async def enqueue(self, function_name: str, *args: object) -> None:
        self.calls.append((function_name, args))


@pytest.fixture
def fake_enqueuer() -> FakeEnqueuer:
    return FakeEnqueuer()


@pytest_asyncio.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession],
    fake_enqueuer: FakeEnqueuer,
) -> AsyncIterator[AsyncClient]:
    from app.python.queue import get_enqueuer

    app = create_app()

    async def override_get_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as s:
            yield s

    app.dependency_overrides[get_session] = override_get_session
    app.dependency_overrides[get_enqueuer] = lambda: fake_enqueuer
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def make_auth_header() -> Iterator[object]:
    def _make(token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    yield _make
