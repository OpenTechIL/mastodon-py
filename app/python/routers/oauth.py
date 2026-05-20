"""`/oauth/*` token-issuance endpoints + `POST /api/v1/apps`."""

from __future__ import annotations

from datetime import UTC
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.python.auth import oauth as oauth_service
from app.python.deps import DBSession
from app.python.settings import get_settings

router = APIRouter(tags=["oauth"])


# ---------- POST /api/v1/apps ----------


class AppCreate(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    client_name: str = Field(..., min_length=1)
    redirect_uris: str = Field(default="urn:ietf:wg:oauth:2.0:oob")
    scopes: str | None = None
    website: str | None = None


class CredentialApp(BaseModel):
    """Returned only once on registration — contains the secret.

    Subsequent `GET /api/v1/apps/verify_credentials` calls return the
    non-credential shape (no client_secret), matching the legacy
    serializer split.
    """

    id: str
    name: str
    website: str | None
    scopes: list[str]
    redirect_uris: list[str]
    redirect_uri: str
    client_id: str
    client_secret: str
    vapid_key: str | None


async def _parse_app_create(request: Request) -> AppCreate:
    """Accept JSON or form-encoded body (both are valid per Mastodon spec)."""
    content_type = request.headers.get("content-type", "")
    try:
        if "application/json" in content_type:
            data = await request.json()
        else:
            form = await request.form()
            data = dict(form)
        return AppCreate.model_validate(data)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()) from exc


@router.post("/api/v1/apps", response_model=CredentialApp, status_code=status.HTTP_200_OK)
async def register(
    session: DBSession,
    body: AppCreate = Depends(_parse_app_create),
) -> CredentialApp:
    """Anonymous endpoint — anyone can register an OAuth client.

    Mastodon's documented body uses `redirect_uris` (string OR array);
    we accept the string form for now. Multi-URI registration is a
    small follow-up.
    """
    app = await oauth_service.register_application(
        session,
        client_name=body.client_name,
        redirect_uri=body.redirect_uris,
        scopes=body.scopes,
        website=body.website,
    )
    return CredentialApp(
        id=str(app.id),
        name=app.name,
        website=app.website or None,
        scopes=app.scopes.split() if app.scopes else [],
        redirect_uris=app.redirect_uris,
        redirect_uri=app.redirect_uri,
        client_id=app.uid,
        client_secret=app.secret,
        vapid_key=get_settings().vapid_public_key,
    )


# ---------- POST /oauth/token ----------


class TokenResponse(BaseModel):
    access_token: str
    token_type: str
    scope: str
    created_at: int


@router.post("/oauth/token", response_model=TokenResponse, status_code=status.HTTP_200_OK)
async def token(
    session: DBSession,
    grant_type: str = Form(...),
    client_id: str = Form(...),
    client_secret: str = Form(...),
    scope: str | None = Form(default=None),
    username: str | None = Form(default=None),
    password: str | None = Form(default=None),
    otp_attempt: str | None = Form(default=None),
) -> TokenResponse:
    """OAuth token endpoint.

    Supported grants:
      - `client_credentials` — application-bound token, no user.
      - `password`           — exchange email + password (+ optional OTP)
                               for a user-bound token.

    `authorization_code` and `refresh_token` return `unsupported_grant_type`
    until those slices port.
    """
    try:
        if grant_type == "client_credentials":
            access_token = await oauth_service.client_credentials_grant(
                session, client_id=client_id, client_secret=client_secret, scope=scope
            )
        elif grant_type == "password":
            if username is None or password is None:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    detail={"error": "invalid_request"},
                )
            access_token = await oauth_service.password_grant(
                session,
                client_id=client_id,
                client_secret=client_secret,
                username=username,
                password=password,
                otp_attempt=otp_attempt,
                scope=scope,
            )
        else:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail={"error": "unsupported_grant_type"},
            )
    except oauth_service.InvalidClient as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail={"error": "invalid_client"}) from exc
    except oauth_service.InvalidGrant as exc:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail={"error": "invalid_grant"},
        ) from exc
    except oauth_service.MFARequired as exc:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": "mfa_required",
                "error_description": "Missing 2FA code",
            },
        ) from exc

    created_at = access_token.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    return TokenResponse(
        access_token=access_token.token,
        token_type="Bearer",
        scope=access_token.scopes or oauth_service.DEFAULT_SCOPES,
        created_at=int(created_at.timestamp()),
    )


# ---------- POST /oauth/revoke ----------


@router.post("/oauth/revoke", status_code=status.HTTP_200_OK)
async def revoke(
    session: DBSession,
    token: str = Form(...),
    client_id: str | None = Form(default=None),
    client_secret: str | None = Form(default=None),
) -> dict[str, Any]:
    """RFC 7009 revoke endpoint: always 200, even on unknown token."""
    await oauth_service.revoke_token(session, raw_token=token, client_id=client_id, client_secret=client_secret)
    return {}
