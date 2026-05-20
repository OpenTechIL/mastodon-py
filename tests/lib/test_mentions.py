"""Unit tests for the mention extractor."""

from __future__ import annotations

import pytest

from app.python.lib import mentions


@pytest.mark.parametrize(
    "text,expected",
    [
        ("", []),
        ("no mentions here", []),
        ("hello @alice", [("alice", None)]),
        ("@alice @bob", [("alice", None), ("bob", None)]),
        ("dup @alice @alice", [("alice", None)]),
        ("remote @bob@other.social hi", [("bob", "other.social")]),
        ("@Alice and @ALICE", [("alice", None)]),  # case-folded + de-duped
        ("punctuation @alice!", [("alice", None)]),
        ("email user@example.com inline", []),  # email-like not matched
        ("@alice @bob@remote.x", [("alice", None), ("bob", "remote.x")]),
        ("hyphen @cool-name", [("cool", None)]),  # hyphen ends username
    ],
)
def test_extract(text: str, expected: list[tuple[str, str | None]]) -> None:
    assert mentions.extract(text) == expected
