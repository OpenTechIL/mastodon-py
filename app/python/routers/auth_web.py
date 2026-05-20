"""`/auth/*` — browser-facing HTML auth pages.

Provides sign-in / sign-up / sign-out for the React SPA.  After a
successful login the server sets a signed session cookie that
`web.py`'s `_build_initial_state` reads to embed the bearer token.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime
from typing import Any

import bcrypt
from fastapi import APIRouter, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select

from app.python.common.snowflake import now_id
from app.python.db import session_factory
from app.python.models import Account, OAuthAccessToken, User
from app.python.models.account_stat import AccountStat
from app.python.settings import get_settings

router = APIRouter(tags=["auth_web"])

_COOKIE_NAME = "_mastodon_session"
_COOKIE_MAX_AGE = 60 * 60 * 24 * 14  # 2 weeks


# ── cookie helpers ────────────────────────────────────────────────────────────


def _sign(payload: str, secret: str) -> str:
    sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def _unsign(signed: str, secret: str) -> str | None:
    if "." not in signed:
        return None
    payload, _, sig = signed.rpartition(".")
    expected = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    return payload


def _set_session(response: Response, user_id: int, account_id: int, token: str) -> None:
    secret = get_settings().secret_key_base or "dev-secret"
    data = json.dumps({"user_id": user_id, "account_id": account_id, "token": token})
    import base64

    encoded = base64.urlsafe_b64encode(data.encode()).decode()
    signed = _sign(encoded, secret)
    response.set_cookie(
        _COOKIE_NAME,
        signed,
        max_age=_COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
    )


def _clear_session(response: Response) -> None:
    response.delete_cookie(_COOKIE_NAME)


def get_session_data(request: Request) -> dict[str, Any] | None:
    """Called from web.py to read the current user's session."""
    cookie = request.cookies.get(_COOKIE_NAME)
    if not cookie:
        return None
    secret = get_settings().secret_key_base or "dev-secret"
    payload = _unsign(cookie, secret)
    if not payload:
        return None
    try:
        import base64

        data = json.loads(base64.urlsafe_b64decode(payload.encode()).decode())
        return data
    except Exception:
        return None


# ── HTML page helpers ─────────────────────────────────────────────────────────


def _page(title: str, body: str) -> str:
    s = get_settings()
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>{title} — {s.local_domain}</title>
  <style>
    *,*::before,*::after{{box-sizing:border-box}}
    body{{margin:0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
          background:#191b22;color:#9baec8;display:flex;align-items:center;
          justify-content:center;min-height:100vh}}
    .card{{background:#282c37;border-radius:8px;padding:40px;width:100%;max-width:400px;
           box-shadow:0 4px 24px rgba(0,0,0,.4)}}
    h1{{margin:0 0 24px;color:#d9e1e8;font-size:1.4rem;text-align:center}}
    label{{display:block;margin-bottom:4px;font-size:.875rem}}
    input[type=email],input[type=password],input[type=text]{{
      width:100%;padding:10px 12px;background:#313543;border:1px solid #42485a;
      border-radius:4px;color:#d9e1e8;font-size:1rem;margin-bottom:16px;outline:none}}
    input:focus{{border-color:#6364ff}}
    .btn{{display:block;width:100%;padding:12px;background:#6364ff;color:#fff;
           border:none;border-radius:4px;font-size:1rem;cursor:pointer;
           text-align:center;text-decoration:none;margin-top:8px}}
    .btn:hover{{background:#7477ff}}
    .btn-secondary{{background:transparent;border:1px solid #6364ff;color:#6364ff;
                     margin-top:8px}}
    .error{{color:#e96c6c;background:#3d1f1f;border:1px solid #7a2020;
             border-radius:4px;padding:10px 12px;margin-bottom:16px;font-size:.875rem}}
    .links{{text-align:center;margin-top:20px;font-size:.875rem}}
    .links a{{color:#6364ff;text-decoration:none}}
  </style>
</head>
<body>
  <div class="card">
    <h1>{title}</h1>
    {body}
  </div>
</body>
</html>"""


# ── sign in ───────────────────────────────────────────────────────────────────


def _sign_in_form(error: str = "") -> str:
    err_html = f'<div class="error">{error}</div>' if error else ""
    return _page(
        "Sign in to Mastodon",
        f"""
    {err_html}
    <form method="post">
      <label for="email">Email or username</label>
      <input type="text" id="email" name="email" autocomplete="email" autofocus/>
      <label for="password">Password</label>
      <input type="password" id="password" name="password" autocomplete="current-password"/>
      <button type="submit" class="btn">Sign in</button>
    </form>
    <div class="links">
      <a href="/auth/sign_up">Create account</a>
    </div>
    """,
    )


@router.get("/auth/sign_in", response_class=HTMLResponse)
async def sign_in_get() -> HTMLResponse:
    return HTMLResponse(_sign_in_form())


@router.post("/auth/sign_in")
async def sign_in_post(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
) -> Response:
    async with session_factory()() as session:
        # accept email or username@domain
        if "@" in email and "." in email.rsplit("@", maxsplit=1)[-1]:
            stmt = select(User).where(User.email == email)
        else:
            # treat as username — look up account first
            stmt = (
                select(User)
                .join(Account, Account.id == User.account_id)
                .where(Account.username == email, Account.domain.is_(None))
            )
        user = (await session.execute(stmt)).scalar_one_or_none()

        if user is None or not user.encrypted_password:
            return HTMLResponse(_sign_in_form("Invalid email or password."), status_code=401)

        try:
            match = bcrypt.checkpw(password.encode(), user.encrypted_password.encode())
        except Exception:
            match = False

        if not match:
            return HTMLResponse(_sign_in_form("Invalid email or password."), status_code=401)

        if user.disabled:
            return HTMLResponse(_sign_in_form("Your account has been disabled."), status_code=403)

        # Find or create an access token for the web app
        now = datetime.now(tz=UTC).replace(tzinfo=None)
        existing_token = (
            await session.execute(
                select(OAuthAccessToken)
                .where(
                    OAuthAccessToken.resource_owner_id == user.id,
                    OAuthAccessToken.application_id.is_(None),
                    OAuthAccessToken.revoked_at.is_(None),
                )
                .limit(1)
            )
        ).scalar_one_or_none()

        if existing_token:
            token = existing_token
        else:
            import secrets

            token = OAuthAccessToken(
                id=now_id(),
                token=secrets.token_hex(32),
                refresh_token=None,
                scopes="read write follow push",
                application_id=None,
                resource_owner_id=user.id,
                expires_in=None,
                revoked_at=None,
                created_at=now,
                last_used_at=None,
                last_used_ip=None,
            )
            session.add(token)
            await session.commit()
            await session.refresh(token)

    redirect = RedirectResponse("/web/home", status_code=302)
    _set_session(redirect, user.id, user.account_id, token.token)
    return redirect


# ── sign out ──────────────────────────────────────────────────────────────────


@router.get("/auth/sign_out")
@router.post("/auth/sign_out")
async def sign_out() -> Response:
    response = RedirectResponse("/", status_code=302)
    _clear_session(response)
    return response


# ── sign up ───────────────────────────────────────────────────────────────────


def _sign_up_form(error: str = "", values: dict | None = None) -> str:
    v = values or {}
    err_html = f'<div class="error">{error}</div>' if error else ""
    return _page(
        "Create account",
        f"""
    {err_html}
    <form method="post">
      <label for="username">Username</label>
      <input type="text" id="username" name="username"
             value="{v.get("username", "")}" autocomplete="username" autofocus/>
      <label for="email">Email</label>
      <input type="email" id="email" name="email"
             value="{v.get("email", "")}" autocomplete="email"/>
      <label for="password">Password (min 8 chars)</label>
      <input type="password" id="password" name="password" autocomplete="new-password"/>
      <button type="submit" class="btn">Create account</button>
    </form>
    <div class="links">
      <a href="/auth/sign_in">Already have an account?</a>
    </div>
    """,
    )


@router.get("/auth/sign_up", response_class=HTMLResponse)
async def sign_up_get() -> HTMLResponse:
    return HTMLResponse(_sign_up_form())


@router.post("/auth/sign_up")
async def sign_up_post(
    username: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
) -> Response:
    values = {"username": username, "email": email}

    if len(password) < 8:
        return HTMLResponse(_sign_up_form("Password must be at least 8 characters.", values), status_code=422)

    async with session_factory()() as session:
        existing = (
            await session.execute(select(Account).where(Account.username == username, Account.domain.is_(None)))
        ).scalar_one_or_none()
        if existing:
            return HTMLResponse(_sign_up_form("Username is already taken.", values), status_code=422)

        existing_user = (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()
        if existing_user:
            return HTMLResponse(_sign_up_form("Email is already in use.", values), status_code=422)

        settings = get_settings()
        now = datetime.now(tz=UTC).replace(tzinfo=None)
        domain = settings.local_domain

        account = Account(
            id=now_id(),
            username=username,
            domain=None,
            display_name="",
            note="",
            uri="",
            url=None,
            locked=False,
            discoverable=False,
            indexable=False,
            memorial=False,
            fields=[],
            public_key="",
            private_key="",
            inbox_url="",
            shared_inbox_url="",
            header_remote_url="",
            created_at=now,
            updated_at=now,
        )
        session.add(account)
        await session.flush()

        account.uri = f"https://{domain}/users/{username}"
        account.url = f"https://{domain}/@{username}"
        account.inbox_url = f"https://{domain}/users/{username}/inbox"

        stat = AccountStat(
            account_id=account.id,
            statuses_count=0,
            following_count=0,
            followers_count=0,
            created_at=now,
            updated_at=now,
        )
        session.add(stat)

        encrypted_password = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(12)).decode("utf-8")

        user = User(
            id=now_id(),
            account_id=account.id,
            email=email,
            encrypted_password=encrypted_password,
            confirmed_at=now,
            approved=True,
            disabled=False,
            otp_required_for_login=False,
            locale="en",
            created_at=now,
            updated_at=now,
        )
        session.add(user)
        await session.flush()

        import secrets

        access_token = OAuthAccessToken(
            id=now_id(),
            token=secrets.token_hex(32),
            refresh_token=None,
            scopes="read write follow push",
            application_id=None,
            resource_owner_id=user.id,
            expires_in=None,
            revoked_at=None,
            created_at=now,
            last_used_at=None,
            last_used_ip=None,
        )
        session.add(access_token)
        await session.commit()
        await session.refresh(access_token)
        await session.refresh(user)

    redirect = RedirectResponse("/web/home", status_code=302)
    _set_session(redirect, user.id, account.id, access_token.token)
    return redirect
