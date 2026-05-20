"""Password verification compatible with Devise's `encrypted_password`.

Devise stores bcrypt hashes with the standard `$2a$<cost>$<salt+hash>`
format. We verify directly via the `bcrypt` package — no password is
ever logged or returned; verify-only by design.

Password *issuance* (sign-up, reset, change) lands when the registration
flow is ported. This module is intentionally read-only until then.
"""

from __future__ import annotations

import bcrypt


def verify(plaintext: str, encrypted: str) -> bool:
    if not plaintext or not encrypted:
        return False
    try:
        return bcrypt.checkpw(plaintext.encode("utf-8"), encrypted.encode("utf-8"))
    except ValueError:
        return False
