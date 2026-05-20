"""HTML rendering for status content.

The legacy backend's `FormattingHelper` runs status text through a
sizeable pipeline: linkify, mention rewrite, hashtag rewrite, custom-
emoji shortcode substitution, paragraph wrapping, then `loofah`
sanitization. That pipeline ports incrementally — Phase 2 ships a
minimal renderer that handles the structural transforms (newline ->
paragraph break, HTML-entity escaping) so the wire shape is right and
text-only content displays correctly. Mentions, hashtags, links, and
custom-emoji rendering land alongside the data models that feed them.
"""

from __future__ import annotations

import html
import re

_PARAGRAPH_BREAK = re.compile(r"\n{2,}")
_SINGLE_NEWLINE = re.compile(r"\n")


def status_content_format(text: str) -> str:
    if not text:
        return ""
    escaped = html.escape(text, quote=False)
    paragraphs = _PARAGRAPH_BREAK.split(escaped)
    rendered = [f"<p>{_SINGLE_NEWLINE.sub('<br>', para).strip()}</p>" for para in paragraphs if para.strip()]
    return "".join(rendered)


def account_bio_format(note: str) -> str:
    """Profile `note` uses the same minimal renderer for now."""
    return status_content_format(note)
