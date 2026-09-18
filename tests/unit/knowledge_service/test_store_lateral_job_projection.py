"""The store joins the latest job per document through a LATERAL subquery.

Adding a column to the outer select list without adding it to the subquery's
projection only fails once a real PostgreSQL executes the statement, and only
for the query that was touched -- the document list endpoint returned 500 in
production with `column j.stage does not exist` while the suite stayed green.
These assertions check the projections statically, so the guard needs neither a
database nor the knowledge-service dependencies.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterator

REPO_ROOT = Path(__file__).resolve().parents[3]
STORE_PATH = REPO_ROOT / "knowledge-service" / "app" / "store.py"

_LATERAL_JOIN = re.compile(r"LEFT JOIN LATERAL\s*\((.*?)\)\s*j\s*ON TRUE", re.S)
_STRING_LITERAL = re.compile(r'"""(.*?)"""', re.S)
_IDENTIFIER = re.compile(r"\b\w+\b")


def _lateral_joins() -> Iterator[tuple[int, list[str], list[str]]]:
    """Yield (line number, projected columns, outer columns read) per join."""
    source = STORE_PATH.read_text(encoding="utf-8")
    literals = list(_STRING_LITERAL.finditer(source))
    for match in _LATERAL_JOIN.finditer(source):
        projection = match.group(1).split("FROM", 1)[0]
        projected = [token for token in _IDENTIFIER.findall(projection) if token != "SELECT"]
        literal = next(
            (item for item in literals if item.start() <= match.start() < item.end()),
            None,
        )
        statement = literal.group(1) if literal else source
        referenced = sorted(set(re.findall(r"\bj\.(\w+)", statement)))
        yield source[: match.start()].count("\n") + 1, projected, referenced


def test_lateral_job_joins_are_discovered():
    """A regex that stops matching would silently disable the guard below."""
    assert list(_lateral_joins()), f"no LATERAL jobs join found in {STORE_PATH}"


def test_lateral_projection_covers_every_referenced_job_column():
    offenders = [
        f"store.py:{line} reads j.{column} but the LATERAL projection only has "
        f"{', '.join(projected)}"
        for line, projected, referenced in _lateral_joins()
        for column in referenced
        if column not in projected
    ]
    assert not offenders, "\n".join(offenders)
