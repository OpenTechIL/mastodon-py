"""`/settings/*` and `/auth/edit` — server-rendered HTML settings pages."""

from __future__ import annotations

from datetime import UTC, datetime

import bcrypt
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select

from app.python.db import session_factory
from app.python.models import Account, CustomFilter, CustomFilterKeyword, User
from app.python.models.account_statuses_cleanup_policy import AccountStatusesCleanupPolicy
from app.python.routers.auth_web import get_session_data

router = APIRouter(tags=["settings"])

_STYLE = """
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: #191b22; color: #9baec8;
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    font-size: 14px; min-height: 100vh;
  }
  .layout { display: flex; min-height: 100vh; }
  .sidebar {
    width: 240px; background: #282c37; padding: 20px 0;
    border-right: 1px solid #393f4f; flex-shrink: 0;
  }
  .sidebar h2 {
    color: #d9e1e8; font-size: 13px; font-weight: 500;
    padding: 8px 20px; text-transform: uppercase; letter-spacing: .05em; margin-bottom: 4px;
  }
  .sidebar a {
    display: block; padding: 8px 20px; color: #9baec8;
    text-decoration: none; border-left: 3px solid transparent;
  }
  .sidebar a:hover, .sidebar a.active {
    background: #313543; color: #d9e1e8; border-left-color: #6364ff;
  }
  .content { flex: 1; padding: 30px 40px; max-width: 780px; }
  h1 { color: #d9e1e8; font-size: 20px; margin-bottom: 24px; }
  h2 { color: #d9e1e8; font-size: 16px; margin: 24px 0 12px; }
  .field { margin-bottom: 18px; }
  label { display: block; color: #d9e1e8; margin-bottom: 6px; font-weight: 500; }
  label .hint { display: block; color: #9baec8; font-weight: 400; font-size: 12px; margin-top: 2px; }
  select, input[type=text], input[type=email], input[type=password], input[type=number] {
    width: 100%; background: #282c37; border: 1px solid #393f4f;
    color: #d9e1e8; padding: 8px 12px; border-radius: 4px; font-size: 14px;
  }
  input[type=number] { width: 120px; }
  input[type=checkbox] { width: 16px; height: 16px; margin-right: 8px; vertical-align: middle; }
  .checkbox-label { display: flex; align-items: center; cursor: pointer; color: #d9e1e8; }
  .btn {
    background: #6364ff; color: #fff; border: none;
    padding: 10px 20px; border-radius: 4px; font-size: 14px; cursor: pointer;
  }
  .btn:hover { background: #7475ff; }
  .btn-danger { background: #df405a; }
  .btn-danger:hover { background: #e55a72; }
  .btn-sm { padding: 5px 10px; font-size: 12px; }
  .alert { background: #3c4b3b; border: 1px solid #4e7b4d; color: #a8d5a2; padding: 10px 14px; border-radius: 4px; margin-bottom: 20px; }
  .error { background: #4b2d2d; border: 1px solid #7b3d3d; color: #d5a2a2; padding: 10px 14px; border-radius: 4px; margin-bottom: 20px; }
  .back { margin-bottom: 20px; }
  .back a { color: #6364ff; text-decoration: none; }
  .back a:hover { text-decoration: underline; }
  table { width: 100%; border-collapse: collapse; margin-bottom: 16px; }
  th { text-align: left; color: #9baec8; font-size: 12px; padding: 6px 8px; border-bottom: 1px solid #393f4f; }
  td { padding: 10px 8px; border-bottom: 1px solid #2c2f3d; vertical-align: middle; }
  td a { color: #6364ff; text-decoration: none; }
  td a:hover { text-decoration: underline; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: 600; }
  .badge-warn { background: #4a3f1a; color: #d4b45a; }
  .badge-hide { background: #3c1a1a; color: #d45a5a; }
  .tag { display: inline-block; background: #393f4f; color: #9baec8; padding: 2px 6px; border-radius: 3px; font-size: 12px; margin: 1px; }
  .filter-card { background: #282c37; border: 1px solid #393f4f; border-radius: 6px; padding: 16px; margin-bottom: 12px; }
  .filter-card-title { color: #d9e1e8; font-weight: 600; font-size: 15px; margin-bottom: 8px; }
  .filter-card-meta { font-size: 12px; color: #9baec8; }
  .filter-card-actions { margin-top: 10px; display: flex; gap: 8px; }
  .keyword-row { display: flex; gap: 8px; align-items: center; margin-bottom: 8px; }
  .keyword-row input[type=text] { flex: 1; }
  .section-title { color: #d9e1e8; font-size: 13px; font-weight: 600; text-transform: uppercase; letter-spacing: .05em; margin-bottom: 12px; padding-bottom: 6px; border-bottom: 1px solid #393f4f; }
  fieldset { border: none; padding: 0; }
  .disabled-info { color: #6c7a90; font-style: italic; font-size: 13px; margin-top: 4px; }
</style>
"""


def _sidebar(active: str) -> str:
    links = [
        ("/settings/profile", "Profile", "profile"),
        ("/settings/preferences/appearance", "Appearance", "appearance"),
        ("/settings/preferences/posting_defaults", "Posting defaults", "posting"),
        ("/settings/preferences/notifications", "Notifications", "notifications"),
        ("/settings/privacy", "Privacy and reach", "privacy"),
        ("/filters", "Filters", "filters"),
        ("/statuses_cleanup", "Automated deletion", "cleanup"),
        ("/auth/edit", "Account", "account"),
        ("/settings/export", "Data export", "export"),
    ]
    items = "".join(
        f'<a href="{href}" class="{"active" if key == active else ""}">{label}</a>' for href, label, key in links
    )
    return f'<div class="sidebar"><h2>Settings</h2>{items}</div>'


def _layout(title: str, content: str, active: str, flash: str = "", error: str = "") -> str:
    flash_html = f'<div class="alert">{flash}</div>' if flash else ""
    error_html = f'<div class="error">{error}</div>' if error else ""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title} · Settings</title>
  {_STYLE}
</head>
<body>
  <div class="layout">
    {_sidebar(active)}
    <div class="content">
      <div class="back"><a href="/web/home">← Back to Mastodon</a></div>
      {flash_html}{error_html}
      {content}
    </div>
  </div>
</body>
</html>"""


def _require_session(request: Request) -> dict | None:
    return get_session_data(request)


# ── /settings redirects ───────────────────────────────────────────────────────


@router.get("/settings", include_in_schema=False)
async def settings_root():
    return RedirectResponse("/settings/preferences/appearance", status_code=302)


@router.get("/settings/preferences", include_in_schema=False)
async def settings_preferences_root():
    return RedirectResponse("/settings/preferences/appearance", status_code=302)


# ── appearance ────────────────────────────────────────────────────────────────


@router.get("/settings/preferences/appearance", response_class=HTMLResponse, include_in_schema=False)
async def preferences_appearance_get(request: Request, saved: str = ""):
    if not _require_session(request):
        return RedirectResponse("/auth/sign_in", status_code=302)
    flash = "Preferences saved." if saved == "1" else ""
    content = """
<h1>Appearance</h1>
<form method="post" action="/settings/preferences/appearance">
  <div class="field">
    <label>Interface language</label>
    <select name="locale">
      <option value="en">English</option>
      <option value="he">Hebrew</option>
      <option value="ar">Arabic</option>
      <option value="de">German</option>
      <option value="fr">French</option>
    </select>
  </div>
  <div class="field">
    <label class="checkbox-label">
      <input type="checkbox" name="auto_play_gif" value="1"> Auto-play animated GIFs
    </label>
  </div>
  <div class="field">
    <label class="checkbox-label">
      <input type="checkbox" name="reduce_motion" value="1"> Reduce motion in animations
    </label>
  </div>
  <button class="btn" type="submit">Save changes</button>
</form>"""
    return HTMLResponse(_layout("Appearance", content, "appearance", flash))


@router.post("/settings/preferences/appearance", include_in_schema=False)
async def preferences_appearance_post(request: Request):
    if not _require_session(request):
        return RedirectResponse("/auth/sign_in", status_code=302)
    return RedirectResponse("/settings/preferences/appearance?saved=1", status_code=302)


# ── posting defaults ──────────────────────────────────────────────────────────


@router.get("/settings/preferences/posting_defaults", response_class=HTMLResponse, include_in_schema=False)
async def preferences_posting_get(request: Request, saved: str = ""):
    if not _require_session(request):
        return RedirectResponse("/auth/sign_in", status_code=302)
    flash = "Preferences saved." if saved == "1" else ""
    content = """
<h1>Posting defaults</h1>
<form method="post" action="/settings/preferences/posting_defaults">
  <div class="field">
    <label>Default post privacy</label>
    <select name="visibility">
      <option value="public">Public</option>
      <option value="unlisted">Unlisted</option>
      <option value="private">Followers only</option>
    </select>
  </div>
  <div class="field">
    <label>Default post language</label>
    <select name="language">
      <option value="">Auto-detect</option>
      <option value="en">English</option>
      <option value="he">Hebrew</option>
      <option value="ar">Arabic</option>
    </select>
  </div>
  <div class="field">
    <label class="checkbox-label">
      <input type="checkbox" name="sensitive" value="1"> Always mark media as sensitive
    </label>
  </div>
  <button class="btn" type="submit">Save changes</button>
</form>"""
    return HTMLResponse(_layout("Posting defaults", content, "posting", flash))


@router.post("/settings/preferences/posting_defaults", include_in_schema=False)
async def preferences_posting_post(request: Request):
    if not _require_session(request):
        return RedirectResponse("/auth/sign_in", status_code=302)
    return RedirectResponse("/settings/preferences/posting_defaults?saved=1", status_code=302)


# ── notifications prefs ───────────────────────────────────────────────────────


@router.get("/settings/preferences/notifications", response_class=HTMLResponse, include_in_schema=False)
async def preferences_notifications_get(request: Request, saved: str = ""):
    if not _require_session(request):
        return RedirectResponse("/auth/sign_in", status_code=302)
    flash = "Preferences saved." if saved == "1" else ""
    content = """
<h1>Notifications</h1>
<form method="post" action="/settings/preferences/notifications">
  <div class="field"><label class="checkbox-label"><input type="checkbox" name="follow" value="1" checked> Someone followed you</label></div>
  <div class="field"><label class="checkbox-label"><input type="checkbox" name="favourite" value="1" checked> Someone favourited your post</label></div>
  <div class="field"><label class="checkbox-label"><input type="checkbox" name="reblog" value="1" checked> Someone boosted your post</label></div>
  <div class="field"><label class="checkbox-label"><input type="checkbox" name="mention" value="1" checked> Someone mentioned you</label></div>
  <button class="btn" type="submit">Save changes</button>
</form>"""
    return HTMLResponse(_layout("Notifications", content, "notifications", flash))


@router.post("/settings/preferences/notifications", include_in_schema=False)
async def preferences_notifications_post(request: Request):
    if not _require_session(request):
        return RedirectResponse("/auth/sign_in", status_code=302)
    return RedirectResponse("/settings/preferences/notifications?saved=1", status_code=302)


# ── profile ───────────────────────────────────────────────────────────────────


@router.get("/settings/profile", response_class=HTMLResponse, include_in_schema=False)
async def settings_profile_get(request: Request):
    if not _require_session(request):
        return RedirectResponse("/auth/sign_in", status_code=302)
    content = """
<h1>Profile</h1>
<p>Profile editing is available in the Mastodon web app.
<a href="/web/home" style="color:#6364ff;">Go to home</a> and click Edit Profile.</p>"""
    return HTMLResponse(_layout("Profile", content, "profile"))


# ── export ────────────────────────────────────────────────────────────────────


@router.get("/settings/export", response_class=HTMLResponse, include_in_schema=False)
async def settings_export_get(request: Request):
    if not _require_session(request):
        return RedirectResponse("/auth/sign_in", status_code=302)
    content = "<h1>Data export</h1><p>Data export is not yet available in this version.</p>"
    return HTMLResponse(_layout("Export", content, "export"))


# ── Privacy and reach ─────────────────────────────────────────────────────────


@router.get("/settings/privacy", response_class=HTMLResponse, include_in_schema=False)
async def settings_privacy(request: Request, saved: str = ""):
    session = _require_session(request)
    if not session:
        return RedirectResponse("/auth/sign_in", status_code=302)
    account_id = session.get("account_id")
    async with session_factory()() as db:
        account = (await db.execute(select(Account).where(Account.id == account_id))).scalar_one_or_none()
    if not account:
        return RedirectResponse("/auth/sign_in", status_code=302)

    def _checked(val: bool | None) -> str:
        return " checked" if val else ""

    content = f"""
<h1>Privacy and reach</h1>
<form method="post" action="/settings/privacy">
  <h2 style="margin:20px 0 8px;font-size:16px;color:#d9e1e8">Reach</h2>
  <p style="margin-bottom:12px;color:#9baec8">Control who can find and follow you.</p>
  <div class="field-group">
    <label class="checkbox-label">
      <input type="checkbox" name="discoverable" value="1"{_checked(account.discoverable)}>
      <span>Allow your account to be discovered</span>
    </label>
    <p class="hint">Opt in to directory listing and suggestions. Requires manual approval to be off.</p>
  </div>
  <div class="field-group">
    <label class="checkbox-label">
      <input type="checkbox" name="locked" value="1"{_checked(account.locked)}>
      <span>Require follow requests</span>
    </label>
    <p class="hint">New followers will need your approval before they can follow you.</p>
  </div>
  <h2 style="margin:20px 0 8px;font-size:16px;color:#d9e1e8">Search</h2>
  <p style="margin-bottom:12px;color:#9baec8">Control how your posts appear in search.</p>
  <div class="field-group">
    <label class="checkbox-label">
      <input type="checkbox" name="indexable" value="1"{_checked(account.indexable)}>
      <span>Include public posts in search results</span>
    </label>
    <p class="hint">Allow your public posts to be indexed by full-text search.</p>
  </div>
  <div class="actions">
    <button type="submit">Save changes</button>
  </div>
</form>
<style>
  .field-group {{ margin-bottom:16px; padding:12px; background:#282c37; border-radius:4px; }}
  .checkbox-label {{ display:flex; align-items:center; gap:10px; cursor:pointer; color:#d9e1e8; font-weight:500; }}
  .checkbox-label input[type=checkbox] {{ width:18px; height:18px; accent-color:#6364ff; }}
  .hint {{ margin-top:6px; color:#9baec8; font-size:12px; }}
</style>
"""
    flash = "Changes saved." if saved == "1" else ""
    return HTMLResponse(_layout("Privacy and reach", content, "privacy", flash=flash))


@router.post("/settings/privacy", response_class=HTMLResponse, include_in_schema=False)
async def update_settings_privacy(
    request: Request,
    discoverable: str = Form(default=""),
    locked: str = Form(default=""),
    indexable: str = Form(default=""),
):
    session = _require_session(request)
    if not session:
        return RedirectResponse("/auth/sign_in", status_code=302)
    account_id = session.get("account_id")
    async with session_factory()() as db:
        account = (await db.execute(select(Account).where(Account.id == account_id))).scalar_one_or_none()
        if not account:
            return RedirectResponse("/auth/sign_in", status_code=302)
        account.discoverable = discoverable == "1"
        account.locked = locked == "1"
        account.indexable = indexable == "1"
        await db.commit()
    return RedirectResponse("/settings/privacy?saved=1", status_code=302)


# ── /filters ─────────────────────────────────────────────────────────────────


def _filter_action_label(action: int) -> str:
    return {0: "warn", 1: "hide", 2: "blur"}.get(action, "warn")


def _expiry_label(expires_at: datetime | None) -> str:
    if expires_at is None:
        return "Never"
    now = datetime.now(tz=UTC).replace(tzinfo=None)
    diff = expires_at - now
    if diff.total_seconds() <= 0:
        return "Expired"
    d = diff.days
    h = diff.seconds // 3600
    if d >= 1:
        return f"In {d}d"
    return f"In {h}h"


@router.get("/filters", response_class=HTMLResponse, include_in_schema=False)
async def filters_index(request: Request, saved: str = ""):
    session = _require_session(request)
    if not session:
        return RedirectResponse("/auth/sign_in", status_code=302)
    account_id = session.get("account_id")
    flash = "Filter saved." if saved == "1" else ""

    async with session_factory()() as db:
        filters = (
            (
                await db.execute(
                    select(CustomFilter).where(CustomFilter.account_id == account_id).order_by(CustomFilter.phrase)
                )
            )
            .scalars()
            .all()
        )

        filter_ids = [f.id for f in filters]
        keywords_by_filter: dict[int, list[CustomFilterKeyword]] = {f.id: [] for f in filters}
        if filter_ids:
            kws = (
                (
                    await db.execute(
                        select(CustomFilterKeyword)
                        .where(CustomFilterKeyword.custom_filter_id.in_(filter_ids))
                        .order_by(CustomFilterKeyword.id)
                    )
                )
                .scalars()
                .all()
            )
            for kw in kws:
                keywords_by_filter[kw.custom_filter_id].append(kw)

    cards = ""
    if not filters:
        cards = '<p style="color:#9baec8;">You have no filters yet. <a href="/filters/new" style="color:#6364ff;">Add one</a>.</p>'
    else:
        for f in filters:
            kws = keywords_by_filter[f.id]
            kw_tags = (
                "".join(f'<span class="tag">{"[W] " if kw.whole_word else ""}{kw.keyword}</span>' for kw in kws)
                or '<span style="color:#6c7a90;">no keywords</span>'
            )
            ctx_tags = " ".join(f'<span class="tag">{c}</span>' for c in (f.context or []))
            action = _filter_action_label(f.action)
            expiry = _expiry_label(f.expires_at)
            cards += f"""
<div class="filter-card">
  <div class="filter-card-title">{f.phrase or "(untitled)"}</div>
  <div class="filter-card-meta">
    Action: <span class="badge badge-{"hide" if action == "hide" else "warn"}">{action}</span>
    &nbsp;·&nbsp; Expires: {expiry}
    &nbsp;·&nbsp; Context: {ctx_tags or "—"}
  </div>
  <div style="margin-top:8px;">{kw_tags}</div>
  <div class="filter-card-actions">
    <a href="/filters/{f.id}/edit"><button class="btn btn-sm" type="button">Edit</button></a>
    <form method="post" action="/filters/{f.id}" style="display:inline">
      <input type="hidden" name="_method" value="delete">
      <button class="btn btn-sm btn-danger" type="submit"
        onclick="return confirm('Delete this filter?')">Delete</button>
    </form>
  </div>
</div>"""

    content = f"""
<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:20px;">
  <h1 style="margin:0;">Filters</h1>
  <a href="/filters/new"><button class="btn" type="button">Add filter</button></a>
</div>
{cards}"""
    return HTMLResponse(_layout("Filters", content, "filters", flash))


@router.get("/filters/new", response_class=HTMLResponse, include_in_schema=False)
async def filter_new(request: Request):
    if not _require_session(request):
        return RedirectResponse("/auth/sign_in", status_code=302)
    return HTMLResponse(_layout("New filter", _filter_form(None, []), "filters"))


@router.post("/filters", response_class=HTMLResponse, include_in_schema=False)
async def filter_create(request: Request):
    session = _require_session(request)
    if not session:
        return RedirectResponse("/auth/sign_in", status_code=302)
    account_id = session.get("account_id")
    form = await request.form()
    now = datetime.now(tz=UTC).replace(tzinfo=None)

    phrase = str(form.get("phrase", "")).strip()
    action = int(str(form.get("action", "0")))
    context = [c for c in ["home", "notifications", "public", "thread", "account"] if form.get(f"context_{c}")]
    expires_in = form.get("expires_in", "")
    expires_at = None
    if expires_in:
        from datetime import timedelta

        expires_at = now + timedelta(seconds=int(str(expires_in)))

    keywords_raw = form.getlist("keyword")
    whole_words_raw = set(form.getlist("whole_word_idx"))

    async with session_factory()() as db:
        f = CustomFilter(
            account_id=account_id,
            phrase=phrase,
            action=action,
            context=context,
            expires_at=expires_at,
            created_at=now,
            updated_at=now,
        )
        db.add(f)
        await db.flush()
        for idx, kw_text in enumerate(keywords_raw):
            kw_text = str(kw_text).strip()
            if not kw_text:
                continue
            db.add(
                CustomFilterKeyword(
                    custom_filter_id=f.id,
                    keyword=kw_text,
                    whole_word=str(idx) in whole_words_raw,
                    created_at=now,
                    updated_at=now,
                )
            )
        await db.commit()

    return RedirectResponse("/filters?saved=1", status_code=302)


@router.get("/filters/{filter_id}/edit", response_class=HTMLResponse, include_in_schema=False)
async def filter_edit(request: Request, filter_id: int):
    session = _require_session(request)
    if not session:
        return RedirectResponse("/auth/sign_in", status_code=302)
    account_id = session.get("account_id")

    async with session_factory()() as db:
        f = (
            await db.execute(
                select(CustomFilter).where(CustomFilter.id == filter_id, CustomFilter.account_id == account_id)
            )
        ).scalar_one_or_none()
        if f is None:
            return RedirectResponse("/filters", status_code=302)
        kws = (
            (
                await db.execute(
                    select(CustomFilterKeyword)
                    .where(CustomFilterKeyword.custom_filter_id == filter_id)
                    .order_by(CustomFilterKeyword.id)
                )
            )
            .scalars()
            .all()
        )

    return HTMLResponse(_layout("Edit filter", _filter_form(f, list(kws)), "filters"))


@router.post("/filters/{filter_id}", response_class=HTMLResponse, include_in_schema=False)
async def filter_update_or_delete(request: Request, filter_id: int):
    session = _require_session(request)
    if not session:
        return RedirectResponse("/auth/sign_in", status_code=302)
    account_id = session.get("account_id")
    form = await request.form()
    method = str(form.get("_method", "")).lower()

    async with session_factory()() as db:
        f = (
            await db.execute(
                select(CustomFilter).where(CustomFilter.id == filter_id, CustomFilter.account_id == account_id)
            )
        ).scalar_one_or_none()
        if f is None:
            return RedirectResponse("/filters", status_code=302)

        if method == "delete":
            # Delete keywords first (FK), then filter
            kws = (
                (await db.execute(select(CustomFilterKeyword).where(CustomFilterKeyword.custom_filter_id == filter_id)))
                .scalars()
                .all()
            )
            for kw in kws:
                await db.delete(kw)
            await db.delete(f)
            await db.commit()
            return RedirectResponse("/filters", status_code=302)

        # Update
        now = datetime.now(tz=UTC).replace(tzinfo=None)
        f.phrase = str(form.get("phrase", "")).strip()
        f.action = int(str(form.get("action", "0")))
        f.context = [c for c in ["home", "notifications", "public", "thread", "account"] if form.get(f"context_{c}")]
        expires_in = form.get("expires_in", "")
        if expires_in:
            from datetime import timedelta

            f.expires_at = now + timedelta(seconds=int(str(expires_in)))
        else:
            f.expires_at = None
        f.updated_at = now

        # Replace keywords: delete existing, insert new
        old_kws = (
            (await db.execute(select(CustomFilterKeyword).where(CustomFilterKeyword.custom_filter_id == filter_id)))
            .scalars()
            .all()
        )
        for kw in old_kws:
            await db.delete(kw)
        await db.flush()

        keywords_raw = form.getlist("keyword")
        whole_words_raw = set(form.getlist("whole_word_idx"))
        for idx, kw_text in enumerate(keywords_raw):
            kw_text = str(kw_text).strip()
            if not kw_text:
                continue
            db.add(
                CustomFilterKeyword(
                    custom_filter_id=filter_id,
                    keyword=kw_text,
                    whole_word=str(idx) in whole_words_raw,
                    created_at=now,
                    updated_at=now,
                )
            )
        await db.commit()

    return RedirectResponse("/filters?saved=1", status_code=302)


def _filter_form(f: CustomFilter | None, kws: list[CustomFilterKeyword]) -> str:
    is_edit = f is not None
    action_url = f"/filters/{f.id}" if f is not None else "/filters"
    title = "Edit filter" if is_edit else "New filter"
    phrase = f.phrase if f else ""
    sel_action = f.action if f else 0
    ctx = set(f.context or []) if f else set()

    ctx_html = "".join(
        f'<label class="checkbox-label" style="margin-bottom:6px;">'
        f'<input type="checkbox" name="context_{c}" value="1" {"checked" if c in ctx else ""}> {c.capitalize()}'
        f"</label>"
        for c in ["home", "notifications", "public", "thread", "account"]
    )

    action_opts = "".join(
        f'<option value="{v}" {"selected" if sel_action == v else ""}>{label}</option>'
        for v, label in [(0, "Warn"), (1, "Hide"), (2, "Blur")]
    )

    # Keyword rows
    if not kws:
        kws_html = _keyword_row(0, "", True)
    else:
        kws_html = "".join(_keyword_row(i, kw.keyword, kw.whole_word) for i, kw in enumerate(kws))

    return f"""
<h1>{title}</h1>
<form method="post" action="{action_url}">
  <div class="field">
    <label>Title / name</label>
    <input type="text" name="phrase" value="{phrase}" placeholder="My filter" required>
  </div>
  <div class="field">
    <label>Filter action</label>
    <select name="action">{action_opts}</select>
  </div>
  <div class="field">
    <label>Expires in</label>
    <select name="expires_in">
      <option value="">Never</option>
      <option value="1800">30 minutes</option>
      <option value="3600">1 hour</option>
      <option value="21600">6 hours</option>
      <option value="43200">12 hours</option>
      <option value="86400">1 day</option>
      <option value="604800">1 week</option>
    </select>
  </div>
  <div class="field">
    <label>Context</label>
    {ctx_html}
  </div>
  <div class="field">
    <label>Keywords</label>
    <div id="kw-list">{kws_html}</div>
    <button type="button" class="btn btn-sm" style="margin-top:6px;"
      onclick="addKeyword()">+ Add keyword</button>
  </div>
  <div style="display:flex;gap:10px;margin-top:20px;">
    <button class="btn" type="submit">Save filter</button>
    <a href="/filters"><button class="btn" type="button" style="background:#393f4f;">Cancel</button></a>
  </div>
</form>
<script>
  let kwIdx = {len(kws) if kws else 1};
  function addKeyword() {{
    const row = document.createElement('div');
    row.className = 'keyword-row';
    row.innerHTML = `<input type="text" name="keyword" placeholder="Keyword">
      <label class="checkbox-label" style="white-space:nowrap;">
        <input type="checkbox" name="whole_word_idx" value="${{kwIdx}}"> Whole word
      </label>`;
    kwIdx++;
    document.getElementById('kw-list').appendChild(row);
  }}
</script>"""


def _keyword_row(idx: int, keyword: str, whole_word: bool) -> str:
    chk = "checked" if whole_word else ""
    return (
        f'<div class="keyword-row">'
        f'<input type="text" name="keyword" value="{keyword}" placeholder="Keyword">'
        f'<label class="checkbox-label" style="white-space:nowrap;">'
        f'<input type="checkbox" name="whole_word_idx" value="{idx}" {chk}> Whole word'
        f"</label></div>"
    )


# ── /statuses_cleanup ─────────────────────────────────────────────────────────

_AGE_OPTIONS = [
    (604_800, "1 week"),
    (1_209_600, "2 weeks"),
    (2_629_746, "1 month"),
    (5_259_492, "2 months"),
    (7_889_238, "3 months"),
    (15_778_476, "6 months"),
    (31_556_952, "1 year"),
    (63_113_904, "2 years"),
]


@router.get("/statuses_cleanup", response_class=HTMLResponse, include_in_schema=False)
async def statuses_cleanup_get(request: Request, saved: str = ""):
    session = _require_session(request)
    if not session:
        return RedirectResponse("/auth/sign_in", status_code=302)
    account_id = session.get("account_id")
    flash = "Settings saved." if saved == "1" else ""

    async with session_factory()() as db:
        policy = (
            await db.execute(
                select(AccountStatusesCleanupPolicy).where(AccountStatusesCleanupPolicy.account_id == account_id)
            )
        ).scalar_one_or_none()

    enabled = policy.enabled if policy else False
    age = policy.min_status_age if policy else 1_209_600
    dis = "" if enabled else "disabled"

    age_opts = "".join(
        f'<option value="{v}" {"selected" if age == v else ""}>{label}</option>' for v, label in _AGE_OPTIONS
    )

    def cb(name: str, default: bool) -> str:
        val = getattr(policy, name, default) if policy else default
        chk = "checked" if val else ""
        return f'<input type="checkbox" name="{name}" value="1" {chk} {dis}>'

    def num(name: str) -> str:
        val = getattr(policy, name, None) if policy else None
        v = str(val) if val is not None else ""
        return f'<input type="number" name="{name}" value="{v}" min="1" placeholder="ignore" {dis}>'

    content = f"""
<h1>Automated post deletion</h1>
<form method="post" action="/statuses_cleanup">
  <div class="field">
    <label class="checkbox-label">
      <input type="checkbox" name="enabled" value="1" {"checked" if enabled else ""}
        onchange="toggleCleanup(this.checked)">
      Automatically delete old posts
    </label>
  </div>
  <div id="cleanup-options" style="{"" if enabled else "opacity:.5;pointer-events:none;"}">
    <div class="field">
      <label>Delete posts older than
        <span class="hint">Posts newer than this will never be deleted</span>
      </label>
      <select name="min_status_age" {dis}>{age_opts}</select>
    </div>
    <div class="section-title" style="margin-top:20px;">Exceptions — keep posts that…</div>
    <div class="field"><label class="checkbox-label">{cb("keep_pinned", True)} Are pinned</label></div>
    <div class="field"><label class="checkbox-label">{cb("keep_direct", True)} Are direct messages</label></div>
    <div class="field"><label class="checkbox-label">{cb("keep_self_fav", True)} You have favourited yourself</label></div>
    <div class="field"><label class="checkbox-label">{cb("keep_self_bookmark", True)} You have bookmarked</label></div>
    <div class="field"><label class="checkbox-label">{cb("keep_polls", False)} Have a poll</label></div>
    <div class="field"><label class="checkbox-label">{cb("keep_media", False)} Have media attachments</label></div>
    <div class="section-title" style="margin-top:20px;">Interaction thresholds</div>
    <div class="field">
      <label>Keep if at least this many favourites {num("min_favs")}</label>
    </div>
    <div class="field">
      <label>Keep if at least this many boosts {num("min_reblogs")}</label>
    </div>
  </div>
  <button class="btn" type="submit" style="margin-top:20px;">Save settings</button>
</form>
<script>
  function toggleCleanup(on) {{
    const el = document.getElementById('cleanup-options');
    el.style.opacity = on ? '1' : '.5';
    el.style.pointerEvents = on ? '' : 'none';
    el.querySelectorAll('input,select').forEach(i => i.disabled = !on);
  }}
</script>"""
    return HTMLResponse(_layout("Automated deletion", content, "cleanup", flash))


@router.post("/statuses_cleanup", response_class=HTMLResponse, include_in_schema=False)
async def statuses_cleanup_post(request: Request):
    session = _require_session(request)
    if not session:
        return RedirectResponse("/auth/sign_in", status_code=302)
    account_id = session.get("account_id")
    form = await request.form()
    now = datetime.now(tz=UTC).replace(tzinfo=None)

    enabled = form.get("enabled") == "1"
    min_age = int(str(form.get("min_status_age", "1209600")))

    def get_bool(name: str, default: bool) -> bool:
        return form.get(name) == "1" if enabled else default

    def get_int_or_none(name: str) -> int | None:
        v = str(form.get(name, "")).strip()
        return int(v) if v.isdigit() and int(v) >= 1 else None

    async with session_factory()() as db:
        policy = (
            await db.execute(
                select(AccountStatusesCleanupPolicy).where(AccountStatusesCleanupPolicy.account_id == account_id)
            )
        ).scalar_one_or_none()

        if policy is None:
            policy = AccountStatusesCleanupPolicy(
                account_id=account_id,
                created_at=now,
                updated_at=now,
            )
            db.add(policy)

        policy.enabled = enabled
        policy.min_status_age = min_age
        policy.keep_pinned = get_bool("keep_pinned", True)
        policy.keep_direct = get_bool("keep_direct", True)
        policy.keep_self_fav = get_bool("keep_self_fav", True)
        policy.keep_self_bookmark = get_bool("keep_self_bookmark", True)
        policy.keep_polls = get_bool("keep_polls", False)
        policy.keep_media = get_bool("keep_media", False)
        policy.min_favs = get_int_or_none("min_favs") if enabled else None
        policy.min_reblogs = get_int_or_none("min_reblogs") if enabled else None
        policy.updated_at = now
        await db.commit()

    return RedirectResponse("/statuses_cleanup?saved=1", status_code=302)


# ── /auth/edit ────────────────────────────────────────────────────────────────


@router.get("/auth/edit", response_class=HTMLResponse, include_in_schema=False)
async def auth_edit_get(request: Request, saved: str = ""):
    session = _require_session(request)
    if not session:
        return RedirectResponse("/auth/sign_in", status_code=302)
    user_id = session.get("user_id")
    flash = "Account settings saved." if saved == "1" else ""

    async with session_factory()() as db:
        user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
        if user is None:
            return RedirectResponse("/auth/sign_in", status_code=302)
        account = (await db.execute(select(Account).where(Account.id == user.account_id))).scalar_one_or_none()

    email = user.email or ""
    username = account.username if account else ""

    content = f"""
<h1>Account settings</h1>
<p style="color:#9baec8;margin-bottom:20px;">Logged in as <strong style="color:#d9e1e8;">@{username}</strong></p>
<form method="post" action="/auth/edit">
  <h2>Change email</h2>
  <div class="field">
    <label>Email address</label>
    <input type="email" name="email" value="{email}" autocomplete="email">
  </div>
  <h2>Change password</h2>
  <div class="field">
    <label>Current password</label>
    <input type="password" name="current_password" autocomplete="current-password">
  </div>
  <div class="field">
    <label>New password
      <span class="hint">Leave blank to keep current password. Minimum 8 characters.</span>
    </label>
    <input type="password" name="new_password" autocomplete="new-password" minlength="8">
  </div>
  <div class="field">
    <label>Confirm new password</label>
    <input type="password" name="new_password_confirmation" autocomplete="new-password">
  </div>
  <button class="btn" type="submit">Save changes</button>
</form>"""
    return HTMLResponse(_layout("Account", content, "account", flash))


@router.post("/auth/edit", response_class=HTMLResponse, include_in_schema=False)
async def auth_edit_post(request: Request):
    session = _require_session(request)
    if not session:
        return RedirectResponse("/auth/sign_in", status_code=302)
    user_id = session.get("user_id")
    form = await request.form()
    now = datetime.now(tz=UTC).replace(tzinfo=None)

    new_email = str(form.get("email", "")).strip()
    current_pw = str(form.get("current_password", ""))
    new_pw = str(form.get("new_password", ""))
    new_pw_confirm = str(form.get("new_password_confirmation", ""))

    async with session_factory()() as db:
        user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
        if user is None:
            return RedirectResponse("/auth/sign_in", status_code=302)
        account = (await db.execute(select(Account).where(Account.id == user.account_id))).scalar_one_or_none()
        username = account.username if account else ""

        error = ""
        if new_pw:
            # Verify current password
            if not current_pw:
                error = "Current password is required to set a new password."
            elif not bcrypt.checkpw(current_pw.encode(), user.encrypted_password.encode()):
                error = "Current password is incorrect."
            elif len(new_pw) < 8:
                error = "New password must be at least 8 characters."
            elif new_pw != new_pw_confirm:
                error = "New password and confirmation do not match."

        if error:
            content = f"""
<h1>Account settings</h1>
<p style="color:#9baec8;margin-bottom:20px;">Logged in as <strong style="color:#d9e1e8;">@{username}</strong></p>
<form method="post" action="/auth/edit">
  <h2>Change email</h2>
  <div class="field">
    <label>Email address</label>
    <input type="email" name="email" value="{new_email}" autocomplete="email">
  </div>
  <h2>Change password</h2>
  <div class="field"><label>Current password</label>
    <input type="password" name="current_password" autocomplete="current-password"></div>
  <div class="field"><label>New password</label>
    <input type="password" name="new_password" autocomplete="new-password" minlength="8"></div>
  <div class="field"><label>Confirm new password</label>
    <input type="password" name="new_password_confirmation" autocomplete="new-password"></div>
  <button class="btn" type="submit">Save changes</button>
</form>"""
            return HTMLResponse(_layout("Account", content, "account", error=error))

        # Apply changes
        if new_email and new_email != user.email:
            user.email = new_email
        if new_pw:
            hashed = bcrypt.hashpw(new_pw.encode(), bcrypt.gensalt(rounds=12)).decode()
            user.encrypted_password = hashed
        user.updated_at = now
        await db.commit()

    return RedirectResponse("/auth/edit?saved=1", status_code=302)
