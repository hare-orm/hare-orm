"""A script holding no statement - empty, blank, comments only - runs as nothing on every driver:
asyncpg failed reading the server's empty query response, and ClickHouse refused it."""

import pytest

from tests.testmodels import Author


@pytest.mark.asyncio
@pytest.mark.parametrize("script", ["", "   ", "-- a comment", "/* a comment */", ";"])
async def test_a_script_of_no_statement_runs_as_nothing(db, script):
    connection = Author._meta.connection
    await connection.execute_script(script)
    assert await connection.execute_dicts("SELECT 41 + 1 AS answer") == [{"answer": 42}]
