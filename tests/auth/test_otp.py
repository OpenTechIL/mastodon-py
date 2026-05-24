"""TOTP verification with replay protection."""

from __future__ import annotations

from collections.abc import Iterator

import pyotp
import pytest

from app.python.auth import otp


@pytest.fixture
def frozen_time(monkeypatch: pytest.MonkeyPatch) -> Iterator[float]:
    fixed = 1_700_000_000.0
    monkeypatch.setattr(otp, "_now", lambda: fixed)
    yield fixed


def test_accepts_current_code(frozen_time: float) -> None:
    secret = pyotp.random_base32()
    code = pyotp.TOTP(secret).at(int(frozen_time))
    result = otp.verify(secret, code, previous_timestep=None)
    assert result.valid
    assert result.consumed_timestep is not None


def test_rejects_wrong_code(frozen_time: float) -> None:
    secret = pyotp.random_base32()
    result = otp.verify(secret, "000000", previous_timestep=None)
    assert not result.valid


def test_replay_blocked_when_timestep_consumed(frozen_time: float) -> None:
    secret = pyotp.random_base32()
    totp = pyotp.TOTP(secret)
    timestep = int(frozen_time // totp.interval)
    code = totp.at(int(frozen_time))

    first = otp.verify(secret, code, previous_timestep=None)
    assert first.valid
    second = otp.verify(secret, code, previous_timestep=timestep)
    assert not second.valid


def test_accepts_one_step_drift(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = pyotp.random_base32()
    totp = pyotp.TOTP(secret)
    # Code generated for "now - 30s"; verify "now" should still accept it.
    monkeypatch.setattr(otp, "_now", lambda: 1_700_000_000.0)
    earlier_code = totp.at(int(1_700_000_000.0) - 30)
    result = otp.verify(secret, earlier_code, previous_timestep=None)
    assert result.valid


def test_empty_inputs() -> None:
    assert not otp.verify(None, "123456", previous_timestep=None).valid
    assert not otp.verify("base32secret", "", previous_timestep=None).valid


def test_non_numeric_code() -> None:
    secret = pyotp.random_base32()
    assert not otp.verify(secret, "abcdef", previous_timestep=None).valid


def test_strips_whitespace(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = pyotp.random_base32()
    monkeypatch.setattr(otp, "_now", lambda: 1_700_000_000.0)
    code = pyotp.TOTP(secret).at(int(1_700_000_000.0))
    padded = f" {code[:3]} {code[3:]} "
    assert otp.verify(secret, padded, previous_timestep=None).valid
