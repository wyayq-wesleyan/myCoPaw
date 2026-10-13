# -*- coding: utf-8 -*-
"""PostgreSQL text-safety helpers.

PostgreSQL text columns cannot store NUL (0x00). ``psycopg2`` rejects such a
value client-side, before the statement reaches the server:

    ValueError: A string literal cannot contain NUL (0x00) characters.

Forensics material is where this bites in practice. Tool output read out of
phone-extraction dumps routinely carries embedded NULs, and one such byte aborts
the whole write — the turn fails to persist and the user sees only a generic
"Answers have stopped" / UNKNOWN_AGENT_ERROR, with the real cause buried in the
exception text.

NULs are stripped rather than escaped: the character carries no meaning in
either a transcript or an audit row, and encoding it (as ``\\u0000``) would leak
a literal backslash sequence into what the model and the user read. Removing it
keeps the text on both sides of the byte intact.
"""

from __future__ import annotations

import re
from typing import Any

_NUL_RE = re.compile("\x00")


def strip_nuls(value: Any) -> Any:
    """Return *value* with NUL bytes removed from every string inside it.

    Walks dicts, lists and tuples so a NUL nested inside a structured payload
    (a serialized ``blocks`` list, tool arguments) is handled too, not just
    top-level text. Non-string leaves are returned unchanged, so this is safe
    to apply to a whole row's worth of parameters.
    """
    if isinstance(value, str):
        return _NUL_RE.sub("", value) if "\x00" in value else value
    if isinstance(value, dict):
        return {key: strip_nuls(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [strip_nuls(item) for item in value]
    return value


__all__ = ["strip_nuls"]
