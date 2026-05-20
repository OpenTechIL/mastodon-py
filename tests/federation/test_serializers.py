"""Unit tests for AP outbound serializers."""

from __future__ import annotations

from datetime import datetime

from app.python.federation.serializers import (
    serialize_create_activity,
    serialize_note,
)
from app.python.models import Account, Status, Visibility


def _make_account() -> Account:
    """Local alice. `lib.asset_urls.account_uri` synthesizes the URI
    from settings since `uri` is empty on locals."""
    now = datetime(2026, 5, 19, 12, 0, 0)
    return Account(
        id=1, username="alice", domain=None,
        display_name="Alice", note="", uri="",
        header_remote_url="", public_key="", private_key="",
        inbox_url="", shared_inbox_url="",
        locked=False, indexable=False, memorial=False,
        created_at=now, updated_at=now,
    )


def _make_status(**overrides) -> Status:
    defaults = {
        "id": 100,
        "account_id": 1,
        "text": "hello world",
        "spoiler_text": "",
        "sensitive": False,
        "visibility": Visibility.PUBLIC.value,
        "language": "en",
        "local": True,
        "reply": False,
        "uri": None,
        "url": None,
        "created_at": datetime(2026, 5, 19, 12, 0, 0),
        "updated_at": datetime(2026, 5, 19, 12, 0, 0),
    }
    defaults.update(overrides)
    return Status(**defaults)  # type: ignore[arg-type]


def test_serialize_note_public_audience() -> None:
    account = _make_account()
    status = _make_status(visibility=Visibility.PUBLIC.value)
    note = serialize_note(status, account)
    assert note["type"] == "Note"
    assert note["content"] == "hello world"
    assert note["attributedTo"].endswith("/users/alice")
    # PUBLIC: as:Public in `to`, followers in `cc`.
    assert "https://www.w3.org/ns/activitystreams#Public" in note["to"]
    assert any("followers" in c for c in note["cc"])
    assert note["contentMap"] == {"en": "hello world"}


def test_serialize_note_unlisted_swaps_to_and_cc() -> None:
    account = _make_account()
    status = _make_status(visibility=Visibility.UNLISTED.value)
    note = serialize_note(status, account)
    # UNLISTED: followers in `to`, as:Public in `cc`.
    assert any("followers" in t for t in note["to"])
    assert "https://www.w3.org/ns/activitystreams#Public" in note["cc"]


def test_serialize_note_private_is_followers_only() -> None:
    account = _make_account()
    status = _make_status(visibility=Visibility.PRIVATE.value)
    note = serialize_note(status, account)
    assert any("followers" in t for t in note["to"])
    assert note["cc"] == []


def test_serialize_note_carries_summary_and_sensitive() -> None:
    account = _make_account()
    status = _make_status(spoiler_text="CW: spoiler", sensitive=True)
    note = serialize_note(status, account)
    assert note["summary"] == "CW: spoiler"
    assert note["sensitive"] is True


def test_serialize_note_uses_stored_uri_when_present() -> None:
    """Edited/imported toots may have their own canonical URI already
    on the row — emit that instead of synthesizing one."""
    account = _make_account()
    status = _make_status(uri="https://us.test/users/alice/statuses/orig")
    note = serialize_note(status, account)
    assert note["id"] == "https://us.test/users/alice/statuses/orig"


def test_serialize_create_activity_wraps_note() -> None:
    account = _make_account()
    status = _make_status()
    activity = serialize_create_activity(status, account)
    assert activity["type"] == "Create"
    assert activity["id"].endswith("/activity")
    assert activity["actor"].endswith("/users/alice")
    assert activity["object"]["content"] == "hello world"
    # Top-level audience matches the Note's — peers can audience-route
    # without re-walking the nested object.
    assert activity["to"] == activity["object"]["to"]
    assert activity["cc"] == activity["object"]["cc"]
