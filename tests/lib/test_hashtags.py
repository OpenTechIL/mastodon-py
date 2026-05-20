"""Unit tests for the hashtag extractor."""

from __future__ import annotations

import pytest

from app.python.lib import hashtags


@pytest.mark.parametrize(
    "text,expected",
    [
        ("", []),
        ("nothing here", []),
        ("#foo", ["foo"]),
        ("hello #foo world", ["foo"]),
        ("multi #foo and #bar", ["foo", "bar"]),
        ("dup #foo #foo", ["foo"]),  # de-duped
        ("case #Foo #FOO", ["foo"]),  # case-normalised + de-duped
        ("with-hyphen #web-dev", ["web-dev"]),
        ("under_score #node_app", ["node_app"]),
        ("inline.#nope", []),  # not preceded by whitespace/start
        ("123 #2024",  []),  # pure-numeric not allowed (needs letter lead)
        ("emoji ＃japanese", ["japanese"]),  # full-width hash
    ],
)
def test_extract(text: str, expected: list[str]) -> None:
    assert hashtags.extract(text) == expected
