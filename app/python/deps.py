"""FastAPI dependencies shared across routers."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

import httpx
from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.python.auth.tokens import AuthContext, resolve_bearer
from app.python.db import get_session
from app.python.models import Account, OAuthApplication, User

DBSession = Annotated[AsyncSession, Depends(get_session)]


async def get_http_client() -> AsyncIterator[httpx.AsyncClient]:
    """Outbound HTTP for federation fetches (actor JSON, etc.).

    Per-request client — cost is negligible compared to the actual
    network calls, and tests can override the dep with a fixture
    instead of patching a module global.
    """
    async with httpx.AsyncClient(timeout=10.0) as client:
        yield client


HttpClient = Annotated[httpx.AsyncClient, Depends(get_http_client)]


async def get_bearer_token(
    authorization: Annotated[str | None, Header()] = None,
) -> str | None:
    """Extract the OAuth bearer token from the Authorization header."""
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return token


BearerToken = Annotated[str | None, Depends(get_bearer_token)]


async def get_optional_auth(
    request: Request,
    token: BearerToken,
    session: DBSession,
) -> AuthContext | None:
    """Resolve the bearer token. Returns None for anonymous requests."""
    if token is None:
        return None
    client_ip = request.client.host if request.client else None
    return await resolve_bearer(session, token, client_ip=client_ip)


OptionalAuth = Annotated[AuthContext | None, Depends(get_optional_auth)]


async def get_current_auth(auth: OptionalAuth) -> AuthContext:
    if auth is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail="The access token is invalid",
            headers={"WWW-Authenticate": 'Bearer realm="Mastodon"'},
        )
    return auth


CurrentAuth = Annotated[AuthContext, Depends(get_current_auth)]


async def get_current_user(auth: CurrentAuth) -> User:
    if auth.user is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="This token has no user")
    return auth.user


CurrentUser = Annotated[User, Depends(get_current_user)]


async def get_current_account(auth: CurrentAuth) -> Account:
    if auth.account is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="This token has no account")
    return auth.account


CurrentAccount = Annotated[Account, Depends(get_current_account)]


async def get_current_application(auth: CurrentAuth) -> OAuthApplication:
    if auth.application is None:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, detail="This token has no application"
        )
    return auth.application


CurrentApplication = Annotated[OAuthApplication, Depends(get_current_application)]


def require_scope(scope: str):
    """Build a Depends that requires the access token to hold `scope`.

    Usage:
        @router.post(..., dependencies=[Depends(require_scope("write:statuses"))])
    """

    async def _check(auth: CurrentAuth) -> None:
        if not auth.has_scope(scope):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                detail=f"This action requires the {scope!r} scope",
            )

    return _check
