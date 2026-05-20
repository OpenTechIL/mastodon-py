"""Extract @user / @user@domain mentions from status text.

The legacy backend uses `Account::MENTION_RE` — a complex regex that
handles RFC 5321 local-parts, IDN-encoded domains, and trailing
punctuation. This slice covers the ASCII-and-extended common case:

  - Local mention:  `@username`         → ("username", None)
  - Remote mention: `@username@host`    → ("username", "host")

A negative-lookbehind on word/@ characters prevents matching the
domain part of an email address mid-text. Trailing punctuation isn't
included in the username; the regex stops at non-username chars.

Returns `[(username_lower, domain_lower_or_None)]` in insertion order,
de-duplicated.
"""

from __future__ import annotations

import re

_MENTION_RE = re.compile(r"(?<![\w@])@([A-Za-z0-9_]+)(?:@([A-Za-z0-9_.-]+))?")


def extract(text: str) -> list[tuple[str, str | None]]:
    if not text:
        return []
    seen: dict[tuple[str, str | None], None] = {}
    for username, domain in _MENTION_RE.findall(text):
        key = (username.lower(), domain.lower() if domain else None)
        seen.setdefault(key, None)
    return list(seen)
