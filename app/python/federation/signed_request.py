"""End-to-end signed-request verification for inbound AP traffic.

This is the function inbox handlers actually call. It composes:

  1. `parse_signature_header` — extract keyId from the Signature header.
  2. `resolve_public_key` — local lookup, then HTTP fallback.
  3. `verify_request` — RSA-SHA256 over the canonical signing string,
     plus a Digest match against the body.

On success, returns the verified actor URL (the keyId with its
fragment stripped) so the caller knows whose activity it just received.
Returns None on any failure — the caller responds 401 and moves on.

We don't surface a richer error shape: in production an inbox runs
under flood, and the distinction between "bad signature" and "actor
not found" leaks information without helping anyone.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Mapping
from urllib.parse import urldefrag

from app.python.federation.key_resolver import resolve_public_key
from app.python.federation.signatures import (
    parse_signature_header,
    verify_request,
)

if TYPE_CHECKING:
    import httpx
    from sqlalchemy.ext.asyncio import AsyncSession


async def verify_signed_request(
    *,
    method: str,
    path: str,
    headers: Mapping[str, str],
    body: bytes,
    session: AsyncSession,
    http_client: httpx.AsyncClient,
) -> str | None:
    """Verify the inbound request. Returns the actor URL on success."""
    sig_value: str | None = None
    for k, v in headers.items():
        if k.lower() == "signature":
            sig_value = str(v).strip()
            break
    if not sig_value:
        return None

    try:
        parsed = parse_signature_header(sig_value)
    except ValueError:
        return None

    pem = await resolve_public_key(
        key_id=parsed.key_id, session=session, http_client=http_client
    )
    if pem is None:
        return None

    ok = verify_request(
        method=method,
        path=path,
        headers=headers,
        body=body,
        public_key_pem=pem,
    )
    if not ok:
        return None
    actor_url, _frag = urldefrag(parsed.key_id)
    return actor_url
