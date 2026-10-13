# -*- coding: utf-8 -*-
"""NUL bytes must be removed before text reaches PostgreSQL.

PostgreSQL text columns cannot store NUL (0x00); ``psycopg2`` refuses the value
client-side with ``ValueError: A string literal cannot contain NUL (0x00)
characters.`` before the statement ever reaches the server.

This matters here because agent tool output is forensics material — phone
extraction dumps, chat logs, CSVs read with ``errors="ignore"`` — and those
routinely contain embedded NULs. Before the fix a single NUL aborted the whole
turn's persistence, and the user saw only a generic "Answers have stopped" /
UNKNOWN_AGENT_ERROR with the real cause buried in the message text.

Two orderings are pinned here, because getting either wrong looks like it works
while still leaking the NUL:

* Stripping must happen **before** JSON encoding. ``json.dumps`` renders NUL as
  the ASCII escape ``\\u0000``, which no string-level strip can see and which
  comes back as a real NUL when the row is loaded.
* Refreshed rows need it too: ``update_entry`` rewrites a turn as its tool
  output grows, so it can introduce a NUL the original insert never had.
"""

import json

from agentscope.message import Msg, TextBlock

from qwenpaw.agents.context.scroll.history import _to_json
from qwenpaw.agents.context.scroll.serialize import msg_to_entries
from qwenpaw.constant import QWENPAW_MESSAGE_TAG_KEY
from qwenpaw.utils.text_safety import strip_nuls

NUL = "\x00"


def test_strip_nuls_removes_top_level_nul() -> None:
    assert strip_nuls(f"前{NUL}后") == "前后"


def test_strip_nuls_walks_nested_structures() -> None:
    """A NUL inside a blocks payload must be cleaned too, not just the text."""
    payload = {
        "type": "text",
        "text": f"a{NUL}b",
        "nested": [{"deep": f"c{NUL}d"}],
    }

    assert strip_nuls(payload) == {
        "type": "text",
        "text": "ab",
        "nested": [{"deep": "cd"}],
    }


def test_strip_nuls_leaves_other_text_untouched() -> None:
    """Only the NUL is removed; surrounding characters survive intact."""
    assert strip_nuls("勾小东 / 微信记录\n第二行") == "勾小东 / 微信记录\n第二行"


def test_to_json_removes_nul_before_escaping() -> None:
    """Encoding first would hide the NUL as the ASCII escape ``\\u0000``.

    This is the ordering that matters: a NUL escaped by ``json.dumps`` passes
    any later string-level check and is restored to a real NUL on load.
    """
    encoded = _to_json([{"type": "text", "text": f"a{NUL}b"}])

    assert encoded is not None
    assert "\\u0000" not in encoded
    assert json.loads(encoded)[0]["text"] == "ab"


def test_to_json_handles_plain_string_values() -> None:
    """String columns bypass json.dumps but still need the same guarantee."""
    assert _to_json(f"x{NUL}y") == "xy"


def test_to_json_passes_through_none() -> None:
    """NULL columns must stay NULL rather than becoming the text ``"null"``."""
    assert _to_json(None) is None


def test_row_values_carry_no_nul() -> None:
    """Serialization alone does not clean; the write path does.

    ``msg_to_entries`` is a faithful serializer — it keeps whatever the turn
    contained. Scrubbing belongs at the database boundary, which is what
    ``_insert_row`` / ``_to_json`` provide, so these are the values that must be
    clean.
    """
    msg = Msg(
        name="assistant",
        role="assistant",
        content=[TextBlock(type="text", text=f"文件内容: {NUL} 勾小东")],
        metadata={QWENPAW_MESSAGE_TAG_KEY: "x"},
        id="m-nul",
    )

    entry = msg_to_entries(msg)[0]

    # The serializer keeps the byte...
    assert NUL in (entry.content or "")

    # ...and the write path removes it, in both the text column and the
    # structured ones.
    assert strip_nuls(entry.content) == "文件内容:  勾小东"
    assert NUL not in (_to_json(entry.blocks) or "")
    assert NUL not in (_to_json(strip_nuls(entry.metadata)) or "")
