"""Extract hashtags from status text.

The legacy backend uses a sophisticated Unicode-aware regex
(`Tag::HASHTAG_RE`) that handles CJK, combining marks, and the full-
width `＃` character. This slice covers the ASCII-and-extended common
case: a `#` or `＃` followed by letters/digits/underscores/hyphens, with
at least one letter so pure-numeric tags don't qualify.

Returns the *names* of the hashtags found (lowercase, no `#` prefix),
in the order they appear; duplicates dropped. The full Unicode-aware
implementation lands when the content-rendering phase ports.
"""

from __future__ import annotations

import re

# `(?:^|\s|>)` — start-of-string, whitespace, or end of an HTML tag boundary.
# `[#＃]`     — hash or full-width hash.
# `(...)`    — captured name: letter-led so pure digits don't match.
_HASHTAG_RE = re.compile(
    r"(?:^|(?<=\s)|(?<=>))[#＃]([A-Za-z_][A-Za-z0-9_-]{0,99})"
)


def extract(text: str) -> list[str]:
    if not text:
        return []
    seen: dict[str, None] = {}  # dict for insertion-ordered de-dup
    for match in _HASHTAG_RE.findall(text):
        name = match.lower()
        seen.setdefault(name, None)
    return list(seen)
