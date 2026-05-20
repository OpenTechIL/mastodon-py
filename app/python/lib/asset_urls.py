"""URL builders for account avatar/header attachments.

The legacy backend (kt-paperclip) computes one of two forms per asset:
remote (cached) accounts use `<asset>_remote_url`; local accounts serve
through `/system/<account>/<asset>/<size>/<file_name>` against the asset
host. We replicate just enough of that here to satisfy clients reading
the public timeline; Phase 4 ports the full Paperclip pipeline including
storage backend selection and variant generation.

Missing assets fall back to `/avatars/original/missing.png` etc. — the
same default the React frontend already knows how to handle.
"""

from __future__ import annotations

from typing import Literal

from app.python.models import Account
from app.python.settings import get_settings

AssetKind = Literal["avatar", "header"]
SizeName = Literal["original", "static"]

_MISSING = {
    "avatar": "/avatars/original/missing.png",
    "header": "/headers/original/missing.png",
}


def _asset_host() -> str:
    settings = get_settings()
    host = settings.web_domain or settings.local_domain
    scheme = "https" if settings.env == "production" else "http"
    return f"{scheme}://{host}"


def _url_for(account: Account, kind: AssetKind, size: SizeName) -> str:
    file_name = getattr(account, f"{kind}_file_name")
    remote_url = getattr(account, f"{kind}_remote_url")

    if not file_name and not remote_url:
        return f"{_asset_host()}{_MISSING[kind]}"

    if remote_url:
        return remote_url

    return f"{_asset_host()}/system/accounts/{kind}s/{account.id}/{size}/{file_name}"


def avatar_url(account: Account, *, static: bool = False) -> str:
    return _url_for(account, "avatar", "static" if static else "original")


def header_url(account: Account, *, static: bool = False) -> str:
    return _url_for(account, "header", "static" if static else "original")


def account_uri(account: Account) -> str:
    """Canonical actor URI. For local accounts derived from settings."""
    if account.uri:
        return account.uri
    return f"{_asset_host()}/users/{account.username}"


def account_url(account: Account) -> str:
    """Public-facing profile URL."""
    if account.url:
        return account.url
    return f"{_asset_host()}/@{account.username}"
