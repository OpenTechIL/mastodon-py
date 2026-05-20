"""Serve the React SPA shell for all /web/* routes.

Rails used to serve a HAML template with an embedded `initial-state` JSON
block. This router replicates that: a minimal HTML page that loads the
Vite-built `application` entrypoint and embeds the minimum initial state
the frontend needs to boot.
"""

from __future__ import annotations

import json
import os

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from sqlalchemy import select

from app.python.deps import DBSession
from app.python.settings import get_settings

router = APIRouter(tags=["web"])


async def _load_user_appearance(request: Request, session) -> dict:
    """Return user appearance prefs from web_settings.  Falls back to defaults."""
    from app.python.routers.auth_web import get_session_data  # noqa: PLC0415
    from app.python.models import User  # noqa: PLC0415
    from app.python.models.web_setting import WebSetting  # noqa: PLC0415

    session_data = get_session_data(request)
    if not session_data:
        return {}
    account_id = session_data.get("account_id")
    if not account_id:
        return {}

    user = (
        await session.execute(select(User).where(User.account_id == account_id))
    ).scalar_one_or_none()
    if user is None:
        return {}

    ws = (
        await session.execute(select(WebSetting).where(WebSetting.user_id == user.id))
    ).scalar_one_or_none()
    if ws is None:
        return {}

    return (ws.data or {}).get("appearance", {})


async def _build_initial_state(request: Request, session) -> dict:
    from app.python.routers.auth_web import get_session_data  # noqa: PLC0415
    from app.python.models import User  # noqa: PLC0415
    from app.python.models.web_setting import WebSetting  # noqa: PLC0415

    s = get_settings()
    domain = s.local_domain
    access_token: str | None = None
    me: str | None = None

    session_data = get_session_data(request)
    web_settings_data: dict = {}

    if session_data:
        access_token = session_data.get("token")
        account_id = session_data.get("account_id")
        me = str(account_id) if account_id else None

        if account_id:
            user = (
                await session.execute(select(User).where(User.account_id == account_id))
            ).scalar_one_or_none()
            if user:
                ws = (
                    await session.execute(
                        select(WebSetting).where(WebSetting.user_id == user.id)
                    )
                ).scalar_one_or_none()
                if ws:
                    web_settings_data = ws.data or {}

    appearance = web_settings_data.get("appearance", {})
    reduce_motion = bool(appearance.get("reduceMotion", False))
    auto_play_gif = bool(appearance.get("autoPlayGif", False))

    return {
        "meta": {
            "access_token": access_token,
            "me": me,
            "admin": None,
            "domain": domain,
            "title": domain,
            "version": "4.3.0+python",
            "repository": "https://github.com/OpenTechIL/mastodon-py",
            "source_url": "https://github.com/OpenTechIL/mastodon-py",
            "status_page_url": "",
            "sso_redirect": "",
            "locale": "en",
            "limited_federation_mode": False,
            "single_user_mode": False,
            "registrations_open": True,
            "profile_directory": True,
            "activity_api_enabled": False,
            "search_enabled": True,
            "trends_enabled": True,
            "show_trends": True,
            "landing_page": "about",
            "terms_of_service_enabled": False,
            "local_live_feed_access": "public",
            "remote_live_feed_access": "public",
            "local_topic_feed_access": "public",
            "remote_topic_feed_access": "public",
            "streaming_api_base_url": f"ws://{domain}",
            "mascot": None,
            "reduce_motion": reduce_motion,
            "auto_play_gif": auto_play_gif,
            "display_media": "default",
            "expand_spoilers": False,
            "advanced_layout": False,
            "boost_modal": False,
            "delete_modal": True,
            "use_blurhash": True,
            "use_pending_items": False,
        },
        "compose": {"text": "", "default_privacy": "public"},
        "accounts": {},
        "media_attachments": {"accept_content_types": []},
        # Hydrate the Redux settings store with whatever we saved last time.
        "settings": web_settings_data,
        "languages": [["en", "English", "English"]],
        "critical_updates_pending": False,
        "features": [],
    }


def _asset_tags(env: str) -> str:
    if env == "production":
        return ""
    return """\
<link rel="stylesheet" media="all" id="inert-style" crossorigin="anonymous" href="/packs-dev/styles/entrypoints/inert.scss" />
  <script type="module">
    import RefreshRuntime from '/packs-dev/@react-refresh';
    RefreshRuntime.injectIntoGlobalHook(window);
    window.$RefreshReg$ = () => {};
    window.$RefreshSig$ = () => (type) => type;
    window.__vite_plugin_react_preamble_installed__ = true;
  </script>
  <script type="module" src="/packs-dev/@vite/client"></script>
  <script type="module" crossorigin="anonymous" src="/packs-dev/entrypoints/common.ts"></script>
  <link rel="stylesheet" media="all" crossorigin="anonymous" href="/packs-dev/themes/default" />
  <script type="module" src="/packs-dev/entrypoints/application.ts"></script>"""


def _html(initial_state: dict, color_scheme: str = "auto", high_contrast: bool = False) -> str:
    s = get_settings()
    domain = s.local_domain
    state_json = json.dumps(initial_state)
    asset_tags = _asset_tags(s.env)
    contrast_val = "high" if high_contrast else "default"
    # Set the preference on <html> so theme-selection.js can resolve it
    # ('auto' stays as-is; the inline script resolves it via matchMedia).
    return f"""<!DOCTYPE html>
<html lang="en" data-color-scheme="{color_scheme}" data-contrast="{contrast_val}">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{domain}</title>
  <script id="initial-state" type="application/json">{state_json}</script>
  {asset_tags}
</head>
<body class="app-body">
  <div id="mastodon" class="app-holder" data-props="{{}}" data-component="Mastodon"></div>
  <noscript>
    <p>JavaScript is required to use Mastodon.</p>
  </noscript>
</body>
</html>
"""


@router.get("/web", include_in_schema=False)
@router.get("/web/{path:path}", include_in_schema=False)
async def web_redirect(path: str = "") -> Response:
    """Mirror the original Mastodon Rails route:
    GET /web/(*any) → 302 redirect to /(*any)
    So /web/home → /home, /web/notifications → /notifications, etc.
    The React Router has no /web basename, so it needs the clean path.
    """
    target = f"/{path}" if path else "/"
    return RedirectResponse(url=target, status_code=302)


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
@router.get("/{path:path}", response_class=HTMLResponse, include_in_schema=False)
async def spa_shell(request: Request, session: DBSession, path: str = "") -> Response:
    # Serve root-level static files from public/ before falling to SPA.
    if path and "." in path and "/" not in path:
        file_path = os.path.join("public", path)
        if os.path.isfile(file_path):
            return FileResponse(file_path)

    appearance = await _load_user_appearance(request, session)
    color_scheme = appearance.get("colorScheme", "auto")
    high_contrast = bool(appearance.get("highContrast", False))

    initial_state = await _build_initial_state(request, session)
    return HTMLResponse(_html(initial_state, color_scheme=color_scheme, high_contrast=high_contrast))
