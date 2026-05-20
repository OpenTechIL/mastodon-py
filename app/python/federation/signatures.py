"""HTTP signatures per draft-cavage-http-signatures-10.

This is the variant Mastodon and the wider Fediverse settled on years
ago. The newer RFC 9421 ("HTTP Message Signatures") is incompatible at
the byte level; until Fediverse-wide adoption catches up we have to
speak the legacy dialect.

Wire format example (line-broken for readability — on the wire it's
one header value):

    Signature: keyId="https://example.test/users/alice#main-key",
               algorithm="rsa-sha256",
               headers="(request-target) host date digest content-type",
               signature="<base64 RSA-SHA256 over the canonical string>"

The canonical signing string is each listed header joined by `\\n`:

    (request-target): post /inbox
    host: example.test
    date: Sun, 01 Jan 2026 00:00:00 GMT
    digest: SHA-256=<base64 SHA-256 of body>
    content-type: application/activity+json

`(request-target)` is the only pseudo-header — `method` lowercased,
space, path-with-query.

`sign_request` mutates a headers dict (adding Date + Digest + Signature
when missing) and returns it. `verify_request` parses the Signature
header, rebuilds the canonical string from the headers the sender
listed, and checks the RSA signature.
"""

from __future__ import annotations

import base64
import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import format_datetime

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import (
    RSAPrivateKey,
    RSAPublicKey,
)

# Default header set Mastodon signs. Inbox POSTs include digest +
# content-type; GETs (e.g. fetching an actor) omit those.
DEFAULT_POST_HEADERS: tuple[str, ...] = (
    "(request-target)",
    "host",
    "date",
    "digest",
    "content-type",
)
DEFAULT_GET_HEADERS: tuple[str, ...] = (
    "(request-target)",
    "host",
    "date",
)


@dataclass(frozen=True, slots=True)
class SignatureHeader:
    """Parsed components of a `Signature:` header value."""

    key_id: str
    algorithm: str
    headers: tuple[str, ...]
    signature: bytes  # raw bytes, already base64-decoded


def _digest(body: bytes) -> str:
    """`Digest: SHA-256=<base64 of sha256(body)>` — Mastodon convention."""
    return "SHA-256=" + base64.b64encode(hashlib.sha256(body).digest()).decode("ascii")


def _http_date(now: datetime | None = None) -> str:
    """RFC 7231 IMF-fixdate, e.g. `Sun, 01 Jan 2026 00:00:00 GMT`."""
    when = now or datetime.now(tz=UTC)
    return format_datetime(when, usegmt=True)


def _normalize_header_value(name: str, headers: Mapping[str, str]) -> str:
    """Header lookup is case-insensitive per HTTP."""
    needle = name.lower()
    for k, v in headers.items():
        if k.lower() == needle:
            return str(v).strip()
    raise KeyError(name)


def _canonical_string(
    method: str,
    path: str,
    headers: Mapping[str, str],
    header_list: tuple[str, ...],
) -> str:
    """Build the string the signature signs over.

    `(request-target)` is the only pseudo-header — replaced inline with
    `<method> <path>`. All other entries come from the supplied headers
    dict (case-insensitive lookup, leading/trailing whitespace stripped).
    """
    lines: list[str] = []
    for name in header_list:
        if name == "(request-target)":
            lines.append(f"(request-target): {method.lower()} {path}")
        else:
            try:
                value = _normalize_header_value(name, headers)
            except KeyError as exc:
                raise ValueError(f"signed header {name!r} missing from request") from exc
            lines.append(f"{name.lower()}: {value}")
    return "\n".join(lines)


def sign_request(
    *,
    method: str,
    path: str,
    headers: dict[str, str],
    body: bytes,
    key_id: str,
    private_key_pem: bytes,
    sign_headers: tuple[str, ...] | None = None,
    now: datetime | None = None,
) -> dict[str, str]:
    """Add Date / Digest / Signature headers and return the updated dict.

    `headers` is mutated in place. `body` is hashed for the Digest
    header. `private_key_pem` is a PEM-encoded RSA private key.

    Defaults the signed header list to `DEFAULT_POST_HEADERS` for
    POST/PUT (anything with a body) and `DEFAULT_GET_HEADERS` otherwise.
    """
    pkey = serialization.load_pem_private_key(private_key_pem, password=None)
    if not isinstance(pkey, RSAPrivateKey):
        raise TypeError("private_key_pem must contain an RSA key")

    method_upper = method.upper()
    is_body_method = method_upper in {"POST", "PUT", "PATCH"}
    if sign_headers is None:
        sign_headers = DEFAULT_POST_HEADERS if is_body_method else DEFAULT_GET_HEADERS

    # Auto-populate the headers we control. Callers who want a custom
    # Date or Digest can set them before calling — we only fill gaps.
    headers.setdefault("Date", _http_date(now))
    if "digest" in (h.lower() for h in sign_headers):
        # Only emit the Digest header when it's actually being signed.
        # GET requests skip it (no body).
        headers.setdefault("Digest", _digest(body))

    signing_string = _canonical_string(method_upper, path, headers, sign_headers)
    raw = pkey.sign(
        signing_string.encode("utf-8"),
        padding.PKCS1v15(),
        hashes.SHA256(),
    )
    sig_b64 = base64.b64encode(raw).decode("ascii")
    header_list = " ".join(h.lower() for h in sign_headers)
    headers["Signature"] = f'keyId="{key_id}",algorithm="rsa-sha256",headers="{header_list}",signature="{sig_b64}"'
    return headers


_SIGPART_RE = re.compile(r'(\w+)\s*=\s*"([^"]*)"')


def parse_signature_header(value: str) -> SignatureHeader:
    """Parse a `Signature:` header into its components.

    Forgiving with whitespace and order; strict about the four fields
    Mastodon requires — anything missing raises ValueError.
    """
    parts = dict(_SIGPART_RE.findall(value))
    missing = {"keyId", "algorithm", "headers", "signature"} - parts.keys()
    if missing:
        raise ValueError(f"signature header missing fields: {sorted(missing)}")
    try:
        signature = base64.b64decode(parts["signature"], validate=True)
    except (ValueError, base64.binascii.Error) as exc:  # type: ignore[attr-defined]
        raise ValueError("signature is not valid base64") from exc
    headers_list = tuple(h.strip() for h in parts["headers"].split() if h.strip())
    if not headers_list:
        raise ValueError("signature `headers` field is empty")
    return SignatureHeader(
        key_id=parts["keyId"],
        algorithm=parts["algorithm"],
        headers=headers_list,
        signature=signature,
    )


def verify_request(
    *,
    method: str,
    path: str,
    headers: Mapping[str, str],
    body: bytes,
    public_key_pem: bytes,
) -> bool:
    """Verify the `Signature:` header against `public_key_pem`.

    Re-derives the canonical string from the request's actual headers
    and checks the RSA-SHA256 signature. Also verifies the Digest header
    matches the body when the sender signed it. Returns False on any
    mismatch — no exceptions for the caller to catch on the hot path.
    """
    try:
        sig_value = _normalize_header_value("signature", headers)
    except KeyError:
        return False

    try:
        parsed = parse_signature_header(sig_value)
    except ValueError:
        return False

    if parsed.algorithm.lower() != "rsa-sha256":
        return False

    # Reject the trivial trust gap: if the sender claimed to sign Digest
    # but the body's digest doesn't match, the body was tampered with
    # post-signing. Same idea for content-type — if it was signed and
    # has changed, the request is no longer what the signer authorized.
    if "digest" in (h.lower() for h in parsed.headers):
        try:
            sent_digest = _normalize_header_value("digest", headers)
        except KeyError:
            return False
        if sent_digest != _digest(body):
            return False

    try:
        signing_string = _canonical_string(method, path, headers, parsed.headers)
    except ValueError:
        return False

    pub = serialization.load_pem_public_key(public_key_pem)
    if not isinstance(pub, RSAPublicKey):
        return False
    try:
        pub.verify(
            parsed.signature,
            signing_string.encode("utf-8"),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
    except InvalidSignature:
        return False
    return True
