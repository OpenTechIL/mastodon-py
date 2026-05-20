"""Devise-compatible password verification."""

from __future__ import annotations

import bcrypt

from app.python.auth import passwords


def _hash(plaintext: str) -> str:
    return bcrypt.hashpw(plaintext.encode("utf-8"), bcrypt.gensalt(rounds=4)).decode("utf-8")


def test_verify_round_trip() -> None:
    assert passwords.verify("hunter2", _hash("hunter2"))


def test_verify_rejects_wrong_password() -> None:
    assert not passwords.verify("wrong", _hash("hunter2"))


def test_verify_empty_inputs() -> None:
    assert not passwords.verify("", _hash("hunter2"))
    assert not passwords.verify("hunter2", "")


def test_verify_rejects_garbage_hash() -> None:
    assert not passwords.verify("hunter2", "not-a-bcrypt-hash")


def test_verify_devise_2a_hash() -> None:
    # A real Devise-generated hash uses the `$2a$` prefix; bcrypt accepts both 2a and 2b.
    devise_style = _hash("hunter2").replace("$2b$", "$2a$", 1)
    assert passwords.verify("hunter2", devise_style)
