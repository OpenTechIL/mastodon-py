"""Request body for `POST /api/v1/statuses`.

We accept the same field names the Mastodon API documents. Unknown
fields (scheduled_at, quote_id, allowed_mentions) are tolerated but
ignored — their absence is the source of the deferred behavior. When
those phases port, they widen this schema rather than introducing a
new endpoint.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Visibility = Literal["public", "unlisted", "private", "direct"]


class PollCreate(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    options: list[str] = Field(default_factory=list, alias="options[]")
    expires_in: int = Field(default=86400, ge=300, le=2629746)
    multiple: bool = False
    hide_totals: bool = False


class StatusCreate(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    status: str = Field(default="")
    spoiler_text: str = Field(default="")
    # `None` means "let the server decide" — currently, "infer from spoiler".
    # An explicit `false` from the client still overrides that inference,
    # so callers who want a sensitive post without a spoiler can set true,
    # and callers who want to clear sensitivity send false.
    sensitive: bool | None = None
    visibility: Visibility = "public"
    language: str | None = None
    in_reply_to_id: int | None = None
    media_ids: list[int] = Field(default_factory=list, alias="media_ids[]")
    poll: PollCreate | None = None
