"""Sign + POST an outbound ActivityPub activity to one inbox.

This is the per-recipient unit `ActivityPub::DeliveryWorker` will fan
out over in a later slice. For now we expose a single sign-and-POST
that takes:

  - the activity JSON (a Python dict)
  - the sending local actor (we read its private key)
  - one recipient inbox URL
  - an httpx client

It signs per draft-cavage-http-signatures-10 with the sender's RSA
private key, POSTs to the inbox, and returns whether the recipient
accepted it (2xx). Network and HTTP errors are caught — callers see
False and decide whether to retry, log, or move on.

`Content-Type` is `application/activity+json` and the URI used for
`keyId` is `<sender.uri>#main-key`, matching how our actor JSON
endpoint advertises the same key.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from app.python.federation.signatures import sign_request

if TYPE_CHECKING:
    import httpx

    from app.python.models import Account


_AP_MEDIA_TYPE = "application/activity+json"


async def sign_and_deliver(
    *,
    activity: dict[str, Any],
    sender: Account,
    recipient_inbox_url: str,
    http_client: httpx.AsyncClient,
) -> bool:
    """Sign the activity as `sender` and POST it to `recipient_inbox_url`.

    Returns True iff the recipient responded with a 2xx. Bad inputs
    (missing private key, malformed inbox URL) return False without
    raising — they're a fan-out-time mistake, not a programmer error
    worth crashing the worker over.
    """
    if not sender.private_key or not sender.uri:
        return False

    parsed = urlparse(recipient_inbox_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"

    body = json.dumps(activity).encode("utf-8")
    headers: dict[str, str] = {
        "Host": parsed.netloc,
        "Content-Type": _AP_MEDIA_TYPE,
    }
    sign_request(
        method="POST",
        path=path,
        headers=headers,
        body=body,
        key_id=f"{sender.uri}#main-key",
        private_key_pem=sender.private_key.encode("utf-8"),
    )

    try:
        response = await http_client.post(
            recipient_inbox_url, content=body, headers=headers
        )
    except Exception:
        return False
    return 200 <= response.status_code < 300
