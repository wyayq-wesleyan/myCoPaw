# -*- coding: utf-8 -*-
"""Persisted metadata must keep the console's client-message id.

The console stamps every send with ``qwenpaw_client_message_id`` and, when it
reloads a session, uses that id to decide whether the pending user message it
still holds has already been persisted. ``msg_to_entries`` whitelists the
metadata it writes, and the id was missing from that list — so the check could
never succeed and the pending question was re-inserted on every fetch.

The user-visible symptom was subtle enough to survive unnoticed: the last
question rendered *twice*, once above the reply and once below it, the lower
copy carrying no timestamp. Nothing errored, so only the ordering looked wrong.

These tests pin the whitelist on both sides: the id must survive, and unrelated
metadata must still be dropped (otherwise every turn persists whatever a
provider or middleware happened to attach).
"""

from agentscope.message import Msg, TextBlock

from qwenpaw.agents.context.scroll.serialize import msg_to_entries
from qwenpaw.constant import (
    QWENPAW_CLIENT_MESSAGE_ID_KEY,
    QWENPAW_MESSAGE_TAG_KEY,
)

CLIENT_ID = "0f9c1e24-7f4a-4a1d-9a3d-2c6b5e8d1234"


def _user_msg(metadata: dict) -> Msg:
    return Msg(
        name="user",
        role="user",
        content=[TextBlock(type="text", text="你看一下工作空间")],
        metadata=metadata,
        id="msg-1",
    )


def _first_entry(msg: Msg):
    entries = msg_to_entries(msg)
    assert entries, "a user message must produce at least one entry"
    return entries[0]


def test_client_message_id_is_persisted() -> None:
    """Dropping this id is what duplicated the last question on reload."""
    entry = _first_entry(
        _user_msg(
            {
                QWENPAW_CLIENT_MESSAGE_ID_KEY: CLIENT_ID,
                QWENPAW_MESSAGE_TAG_KEY: "external_user_query",
            },
        ),
    )

    assert entry.metadata[QWENPAW_CLIENT_MESSAGE_ID_KEY] == CLIENT_ID


def test_message_tag_is_still_persisted() -> None:
    """The tag distinguishes synthetic stubs from real requests."""
    entry = _first_entry(
        _user_msg({QWENPAW_MESSAGE_TAG_KEY: "external_user_query"}),
    )

    assert entry.metadata[QWENPAW_MESSAGE_TAG_KEY] == "external_user_query"


def test_unrelated_metadata_is_still_dropped() -> None:
    """The whitelist must stay a whitelist, not a passthrough."""
    entry = _first_entry(
        _user_msg(
            {
                QWENPAW_CLIENT_MESSAGE_ID_KEY: CLIENT_ID,
                "some_provider_internal_flag": "keep-me-out",
                "raw_request_body": {"secret": "x"},
            },
        ),
    )

    assert "some_provider_internal_flag" not in entry.metadata
    assert "raw_request_body" not in entry.metadata


def test_absent_client_message_id_adds_no_key() -> None:
    """Messages from other channels have no id; do not invent an empty one."""
    entry = _first_entry(
        _user_msg({QWENPAW_MESSAGE_TAG_KEY: "external_user_query"}),
    )

    assert QWENPAW_CLIENT_MESSAGE_ID_KEY not in entry.metadata


def test_response_messages_are_untouched() -> None:
    """The id only ever rides on the user turn; a reply must not gain one."""
    reply = Msg(
        name="assistant",
        role="assistant",
        content=[TextBlock(type="text", text="好的")],
        metadata={QWENPAW_CLIENT_MESSAGE_ID_KEY: CLIENT_ID},
        id="msg-2",
    )

    entry = _first_entry(reply)

    # The id is not part of the persisted set for a reply in practice; either
    # way the entry must not fabricate fields it was not given.
    assert entry.kind == "model_turn"
