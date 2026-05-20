"""TOTP verification compatible with `devise-two-factor`.

`users.otp_secret` is the base32-encoded shared secret. The Rails gem
guards against one-step replay by storing the most recently consumed
timestep in `users.consumed_timestep`; we replicate that check so a
sign-in attempt that re-uses a still-valid 30-second OTP can't bypass
2FA. The user's stored secret is read; never written here.
"""

from __future__ import annotations

from dataclasses import dataclass

import pyotp

_DRIFT_STEPS = 1  # accept +/- one 30s window, same as devise-two-factor


@dataclass(slots=True)
class OTPResult:
    valid: bool
    consumed_timestep: int | None


def verify(secret: str | None, code: str, *, previous_timestep: int | None) -> OTPResult:
    if not secret or not code:
        return OTPResult(valid=False, consumed_timestep=None)

    code = code.strip().replace(" ", "")
    if not code.isdigit():
        return OTPResult(valid=False, consumed_timestep=None)

    totp = pyotp.TOTP(secret)
    now_seconds = _now()
    timestep = int(now_seconds // totp.interval)

    for delta in range(-_DRIFT_STEPS, _DRIFT_STEPS + 1):
        candidate = timestep + delta
        if previous_timestep is not None and candidate <= previous_timestep:
            continue
        if pyotp.utils.strings_equal(code, totp.at(candidate * totp.interval)):
            return OTPResult(valid=True, consumed_timestep=candidate)

    return OTPResult(valid=False, consumed_timestep=None)


def _now() -> float:
    import time

    return time.time()
