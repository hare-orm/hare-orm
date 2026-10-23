"""Repro/regression test for the postgres_common/schema_generator.py +
migrations/schema_editor/postgres_common.py comment-escaping finding."""

import pytest

from hare import Connections
from hare.contrib.test import requires_features


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_column_comment_with_newline_round_trips_unescaped(db):
    """The Postgres escape table only overrode the single-quote entry but inherited the rest of
    the base MySQL-style table (backslash-escapes backslash/newline/CR/NUL) via super() - Postgres
    standard string literals don't interpret backslash escapes at all, so a comment containing a
    real newline used to be stored as the literal two characters "\\n" instead of an actual
    newline: not a syntax error, silently wrong data."""
    conn = Connections.get("models")
    query = (
        "SELECT col_description(c.oid, a.attnum) AS comment "
        "FROM pg_class c JOIN pg_attribute a ON a.attrelid = c.oid "
        "WHERE c.relname = 'comments' AND a.attname = 'multiline_comment'"
    )
    rows = await conn.execute_dicts(query)
    comment = rows[0]["comment"] if rows else None
    assert comment == "Some \n comment"
