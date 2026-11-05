"""Opaque statements report columns and counts from the server on both PostgreSQL drivers."""

import pytest

from hare.contrib.test import requires_features
from hare.transactions.transactions import Transactions
from tests.utils.database_under_test import DatabaseUnderTest


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["execute", "execute_described"])
async def test_raw_statement_metadata_comes_from_command_completion(db_simple, method):
    cases = (
        (
            "/* outer /* RETURNING */ comment */ UPDATE statement_metadata SET value = 'changed' WHERE id = 1",
            1,
            (),
            (),
        ),
        ("UPDATE statement_metadata SET value = 'returning' WHERE id = 1", 1, (), ()),
        (
            "WITH selected AS MATERIALIZED (SELECT 2 AS id) "
            "UPDATE statement_metadata SET value = 'changed' WHERE id IN (SELECT id FROM selected)",
            1,
            (),
            (),
        ),
        (
            "WITH changed AS (DELETE FROM statement_metadata WHERE id = 1 RETURNING id) SELECT id FROM changed",
            1,
            ("id",),
            ((1,),),
        ),
        ("UPDATE statement_metadata SET value = 'changed' WHERE id = 1 RETURNING id", 1, ("id",), ((1,),)),
        ("UPDATE statement_metadata SET value = 'changed' WHERE id = 0 RETURNING id", 0, ("id",), ()),
        ("SELECT $text$DELETE RETURNING$text$ AS value", 1, ("value",), (("DELETE RETURNING",),)),
    )
    async with Transactions.atomic() as connection:
        await connection.execute_script("CREATE TEMP TABLE statement_metadata (id integer, value text) ON COMMIT DROP")
        for query, count, columns, expected_rows in cases:
            await connection.execute_script(
                "TRUNCATE statement_metadata; INSERT INTO statement_metadata VALUES (1, 'first'), (2, 'second')"
            )
            result = await getattr(connection, method)(query)
            assert result.row_count == count, query
            if method == "execute_described":
                assert result.columns == columns, query
                rows = result.rows
            else:
                rows = tuple(tuple(row.values()) for row in result.rows)
            assert rows == expected_rows, query


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_schema_editor_invalidates_cached_result_metadata(db_simple):
    if DatabaseUnderTest.pools_server_sessions():
        pytest.skip("checks driver caches on one physical server session")
    async with Transactions.atomic() as connection:
        editor = connection.dialect.schema_editor_class(connection)
        await editor.run_sql("CREATE TEMP TABLE schema_metadata (value integer) ON COMMIT DROP")
        assert await connection.execute_dicts("SELECT * FROM schema_metadata") == []
        await editor.run_sql("ALTER TABLE schema_metadata ADD COLUMN label text")
        await connection.execute("INSERT INTO schema_metadata VALUES (1, 'label')", returns_rows=False)
        assert await connection.execute_dicts("SELECT * FROM schema_metadata") == [{"value": 1, "label": "label"}]
