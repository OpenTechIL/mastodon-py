"""FastAPI app factory."""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError

from app.python.db import dispose_engine
from app.python.routers import accounts as accounts_router
from app.python.routers import activitypub as activitypub_router
from app.python.routers import admin as admin_router
from app.python.routers import auth_web as auth_web_router
from app.python.routers import collections as collections_router
from app.python.routers import directory as directory_router
from app.python.routers import profile as profile_router
from app.python.routers import announcements as announcements_router
from app.python.routers import apps as apps_router
from app.python.routers import conversations as conversations_router
from app.python.routers import featured_tags as featured_tags_router
from app.python.routers import filters as filters_router
from app.python.routers import follow_requests as follow_requests_router
from app.python.routers import instance as instance_router
from app.python.routers import listings as listings_router
from app.python.routers import lists as lists_router
from app.python.routers import media as media_router
from app.python.routers import moderation as moderation_router
from app.python.routers import notifications as notifications_router
from app.python.routers import oauth as oauth_router
from app.python.routers import polls as polls_router
from app.python.routers import search as search_router
from app.python.routers import settings as settings_router
from app.python.routers import startup as startup_router
from app.python.routers import statuses as statuses_router
from app.python.routers import tags as tags_router
from app.python.routers import timelines as timelines_router
from app.python.routers import web as web_router
from app.python.routers import web_api as web_api_router
from app.python.routers import well_known as well_known_router
from app.python.settings import get_settings


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    yield
    await dispose_engine()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Mastodon",
        version="0.0.0",
        lifespan=lifespan,
        docs_url="/_py/docs" if settings.env != "production" else None,
        openapi_url="/_py/openapi.json" if settings.env != "production" else None,
    )

    @app.get("/_py/health", tags=["meta"])
    async def health() -> dict[str, str]:
        return {"status": "ok", "env": settings.env}

    @app.exception_handler(OperationalError)
    @app.exception_handler(InterfaceError)
    @app.exception_handler(DBAPIError)
    @app.exception_handler(ConnectionError)
    async def _db_unavailable(request: Request, exc: Exception) -> JSONResponse:
        """Surface DB-connectivity errors as 503 rather than leaking a
        SQLAlchemy stack trace. The common dev-time cause is Postgres
        not running.

        `ConnectionError` is included because `ConnectionRefusedError`
        (raw OS errno 61) bubbles up from asyncpg before SQLAlchemy
        wraps it as DBAPIError — we have to catch it at the OS layer.
        """
        logging.getLogger(__name__).warning(
            "database unavailable for %s %s: %s",
            request.method, request.url.path, exc.__class__.__name__,
        )
        return JSONResponse(
            status_code=503,
            content={"error": "Database is unavailable"},
        )

    app.include_router(auth_web_router.router)
    app.include_router(admin_router.router)
    app.include_router(apps_router.router)
    app.include_router(accounts_router.router)
    app.include_router(directory_router.router)
    app.include_router(profile_router.router)
    app.include_router(collections_router.router)
    app.include_router(statuses_router.router)
    app.include_router(timelines_router.router)
    app.include_router(notifications_router.router)
    app.include_router(notifications_router.v2_router)
    app.include_router(instance_router.router)
    app.include_router(lists_router.router)
    app.include_router(listings_router.router)
    app.include_router(follow_requests_router.router)
    app.include_router(startup_router.router)
    app.include_router(tags_router.router)
    app.include_router(filters_router.router)
    app.include_router(filters_router.v1_router)
    app.include_router(announcements_router.router)
    app.include_router(featured_tags_router.router)
    app.include_router(moderation_router.router)
    app.include_router(oauth_router.router)
    app.include_router(conversations_router.router)
    app.include_router(settings_router.router)
    app.include_router(search_router.router)
    app.include_router(web_api_router.router)
    app.include_router(media_router.router)
    app.include_router(polls_router.router)
    app.include_router(activitypub_router.router)
    app.include_router(well_known_router.router)
    # Mount specific public subdirectories BEFORE the SPA catch-all so that
    # /sounds/*, /emoji/*, etc. are served as static files rather than HTML.
    for subdir in ("sounds", "emoji", "avatars", "headers", "packs", "system", "ocr"):
        path = os.path.join("public", subdir)
        if os.path.isdir(path):
            app.mount(f"/{subdir}", StaticFiles(directory=path), name=subdir)

    # SPA catch-all — must be last so all API routes and static mounts above
    # take priority. Any unrecognised path returns the React shell.
    app.include_router(web_router.router)

    return app


app = create_app()
