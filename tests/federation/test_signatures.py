"""Tests for HTTP signatures (Cavage draft 10)."""

from __future__ import annotations

import base64
import hashlib
from datetime import datetime, timezone

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.python.federation.signatures import (
    parse_signature_header,
    sign_request,
    verify_request,
)


@pytest.fixture(scope="module")
def rsa_keypair() -> tuple[bytes, bytes]:
    """One 2048-bit RSA key reused across tests in this module."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private_pem, public_pem


def _build_post(rsa_keypair: tuple[bytes, bytes]) -> tuple[dict[str, str], bytes]:
    priv, _pub = rsa_keypair
    headers: dict[str, str] = {
        "Host": "example.test",
        "Content-Type": "application/activity+json",
    }
    body = b'{"@context":"https://www.w3.org/ns/activitystreams","type":"Create"}'
    sign_request(
        method="POST",
        path="/inbox",
        headers=headers,
        body=body,
        key_id="https://example.test/users/alice#main-key",
        private_key_pem=priv,
        now=datetime(2026, 5, 18, 12, 0, 0, tzinfo=timezone.utc),
    )
    return headers, body


def test_sign_request_populates_date_digest_signature(
    rsa_keypair: tuple[bytes, bytes],
) -> None:
    headers, body = _build_post(rsa_keypair)
    assert headers["Date"] == "Mon, 18 May 2026 12:00:00 GMT"
    # Digest is SHA-256 of the body, base64.
    expected_digest = (
        "SHA-256=" + base64.b64encode(hashlib.sha256(body).digest()).decode()
    )
    assert headers["Digest"] == expected_digest
    # Signature is a 4-field comma-separated string.
    sig = headers["Signature"]
    assert 'keyId="https://example.test/users/alice#main-key"' in sig
    assert 'algorithm="rsa-sha256"' in sig
    assert 'headers="(request-target) host date digest content-type"' in sig
    assert 'signature="' in sig


def test_round_trip_verify_passes(rsa_keypair: tuple[bytes, bytes]) -> None:
    _priv, pub = rsa_keypair
    headers, body = _build_post(rsa_keypair)
    assert verify_request(
        method="POST",
        path="/inbox",
        headers=headers,
        body=body,
        public_key_pem=pub,
    )


def test_verify_fails_on_tampered_body(
    rsa_keypair: tuple[bytes, bytes],
) -> None:
    """Digest is signed; if the body is rewritten post-signing, verify
    notices because the body's SHA-256 no longer matches the Digest
    header."""
    _priv, pub = rsa_keypair
    headers, _body = _build_post(rsa_keypair)
    tampered = b'{"@context":"https://www.w3.org/ns/activitystreams","type":"Delete"}'
    assert not verify_request(
        method="POST",
        path="/inbox",
        headers=headers,
        body=tampered,
        public_key_pem=pub,
    )


def test_verify_fails_on_tampered_signature(
    rsa_keypair: tuple[bytes, bytes],
) -> None:
    _priv, pub = rsa_keypair
    headers, body = _build_post(rsa_keypair)
    # Mangle the signature bytes — keep it valid base64 so parsing
    # succeeds but the RSA check fails.
    sig = headers["Signature"]
    bad = sig.replace('signature="', 'signature="AA')
    headers["Signature"] = bad
    assert not verify_request(
        method="POST",
        path="/inbox",
        headers=headers,
        body=body,
        public_key_pem=pub,
    )


def test_verify_fails_with_wrong_public_key(
    rsa_keypair: tuple[bytes, bytes],
) -> None:
    headers, body = _build_post(rsa_keypair)
    # A fresh key pair: the public half won't verify the signature
    # made with the original private key.
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    other_pub = other.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    assert not verify_request(
        method="POST",
        path="/inbox",
        headers=headers,
        body=body,
        public_key_pem=other_pub,
    )


def test_verify_fails_on_path_mismatch(
    rsa_keypair: tuple[bytes, bytes],
) -> None:
    """`(request-target)` carries the path; changing it post-signing
    invalidates the signature."""
    _priv, pub = rsa_keypair
    headers, body = _build_post(rsa_keypair)
    assert not verify_request(
        method="POST",
        path="/inbox/different",
        headers=headers,
        body=body,
        public_key_pem=pub,
    )


def test_verify_fails_on_missing_signature_header(
    rsa_keypair: tuple[bytes, bytes],
) -> None:
    _priv, pub = rsa_keypair
    headers, body = _build_post(rsa_keypair)
    del headers["Signature"]
    assert not verify_request(
        method="POST",
        path="/inbox",
        headers=headers,
        body=body,
        public_key_pem=pub,
    )


def test_get_request_uses_get_headers_default(
    rsa_keypair: tuple[bytes, bytes],
) -> None:
    """GETs default to signing only (request-target), host, date — no
    digest or content-type, since there's no body."""
    priv, pub = rsa_keypair
    headers: dict[str, str] = {"Host": "example.test"}
    sign_request(
        method="GET",
        path="/users/alice",
        headers=headers,
        body=b"",
        key_id="https://example.test/users/alice#main-key",
        private_key_pem=priv,
    )
    assert "Digest" not in headers
    assert 'headers="(request-target) host date"' in headers["Signature"]
    assert verify_request(
        method="GET",
        path="/users/alice",
        headers=headers,
        body=b"",
        public_key_pem=pub,
    )


def test_parse_signature_header_extracts_fields() -> None:
    raw = (
        'keyId="https://example.test/users/alice#main-key",'
        'algorithm="rsa-sha256",'
        'headers="(request-target) host date",'
        'signature="' + base64.b64encode(b"x" * 256).decode() + '"'
    )
    parsed = parse_signature_header(raw)
    assert parsed.key_id == "https://example.test/users/alice#main-key"
    assert parsed.algorithm == "rsa-sha256"
    assert parsed.headers == ("(request-target)", "host", "date")
    assert parsed.signature == b"x" * 256


def test_parse_signature_header_rejects_missing_fields() -> None:
    with pytest.raises(ValueError, match="missing fields"):
        parse_signature_header('keyId="x",algorithm="rsa-sha256"')


def test_parse_signature_header_rejects_bad_base64() -> None:
    with pytest.raises(ValueError, match="base64"):
        parse_signature_header(
            'keyId="x",algorithm="rsa-sha256",headers="date",signature="!!!"'
        )


def test_verify_rejects_unsupported_algorithm(
    rsa_keypair: tuple[bytes, bytes],
) -> None:
    """We pin RSA-SHA256; any other algorithm string is refused even if
    the signature would otherwise verify."""
    _priv, pub = rsa_keypair
    headers, body = _build_post(rsa_keypair)
    headers["Signature"] = headers["Signature"].replace(
        'algorithm="rsa-sha256"', 'algorithm="hmac-sha256"'
    )
    assert not verify_request(
        method="POST",
        path="/inbox",
        headers=headers,
        body=body,
        public_key_pem=pub,
    )
