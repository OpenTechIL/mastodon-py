"""REST shape for `OAuthApplication`.

Mirrors the Mastodon API's `Application` entity. `redirect_uri` is the
historical (pre-4.3) field; `redirect_uris` is its list-valued
replacement; both are emitted because clients in the wild still read
either one. `vapid_key` is deprecated upstream but still emitted until
the API contract drops it.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, computed_field

from app.python.models import OAuthApplication
from app.python.settings import get_settings


class Application(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    website: str | None = None
    scopes: list[str] = Field(default_factory=list)
    redirect_uris: list[str] = Field(default_factory=list)
    redirect_uri: str

    @computed_field  # type: ignore[prop-decorator]
    @property
    def vapid_key(self) -> str | None:
        return get_settings().vapid_public_key

    @classmethod
    def from_model(cls, app: OAuthApplication) -> Application:
        return cls(
            id=str(app.id),
            name=app.name,
            website=app.website or None,
            scopes=app.scopes.split() if app.scopes else [],
            redirect_uris=app.redirect_uris,
            redirect_uri=app.redirect_uri,
        )
