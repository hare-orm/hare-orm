"""Opaque statements report columns and counts from the server on both PostgreSQL drivers."""

import pytest

from hare import Connections
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


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["savepoint", "rollback", "commit"])
async def test_native_type_metadata_follows_transaction_visibility(db_simple, boundary):
    if DatabaseUnderTest.pools_server_sessions():
        pytest.skip("checks driver caches on physical server sessions")
    client = Connections.current().create_independent("models", {"min_size": 1, "max_size": 2})
    writer = reader = None
    try:
        if client.driver_name != "postgresql":
            pytest.skip("direct native transaction API")
        await client._ensure_connection()
        pool = client._connected_pool
        await client.execute_script(
            "CREATE TYPE visibility_shape AS (value integer);"
            "CREATE TABLE visibility_payload(value visibility_shape);"
            "INSERT INTO visibility_payload VALUES(ROW(7)::visibility_shape)"
        )
        pool.invalidate_schema_cache()
        writer = await pool.begin(None)
        assert tuple((await writer.fetch_all("SELECT value FROM visibility_payload", []))[0]["value"]) == (7,)
        if boundary == "savepoint":
            await writer.savepoint("type_change")
        await writer.execute_script("ALTER TYPE visibility_shape ADD ATTRIBUTE label text")
        writer.invalidate_schema_cache()
        assert tuple((await writer.fetch_all("SELECT value FROM visibility_payload", []))[0]["value"]) == (7, None)
        expected = (7,)
        if boundary == "savepoint":
            await writer.rollback_to("type_change")
            target = writer
        elif boundary == "rollback":
            await writer.rollback()
            writer = None
            target = pool
        else:
            reader = await pool.begin(None)
            assert (await reader.fetch_all("SELECT NULL::visibility_shape AS value", []))[0]["value"] is None
            await writer.commit()
            writer = None
            target, expected = reader, (7, None)
        assert tuple((await target.fetch_all("SELECT value FROM visibility_payload", []))[0]["value"]) == expected
    finally:
        if reader is not None:
            await reader.rollback()
        if writer is not None:
            await writer.rollback()
        if client.driver_name == "postgresql":
            await client.execute_script(
                "DROP TABLE IF EXISTS visibility_payload; DROP TYPE IF EXISTS visibility_shape"
            )
        await client.close()


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
@pytest.mark.parametrize("reader", ["dicts", "described", "stream"])
async def test_anonymous_record_fields_keep_their_domain_array_types(db_simple, reader):
    domain_name = f"record_numbers_{reader}"
    async with Transactions.atomic() as connection:
        await connection.execute_script(f"CREATE DOMAIN {domain_name} AS integer[]")
        await connection.invalidate_schema_cache()
        query = (
            f"SELECT ARRAY[2,NULL,4]::{domain_name} AS direct,"
            f"ROW(ARRAY[2,NULL,4]::{domain_name}) AS nested,"
            "ROW(ARRAY[2,NULL,4]::integer[]) AS builtin"
        )
        for _ in range(2):
            if reader == "stream":
                stream = connection.stream(query)
                try:
                    rows = [dict(row) async for row in stream]
                finally:
                    await stream.aclose()
            elif reader == "described":
                result = await connection.execute_described(query)
                rows = [dict(zip(result.columns, row, strict=True)) for row in result.rows]
            else:
                rows = await connection.execute_dicts(query)
            assert rows[0]["direct"] == [2, None, 4]
            assert tuple(rows[0]["builtin"]) == ([2, None, 4],)
            assert tuple(rows[0]["nested"]) == ([2, None, 4],)
        await connection.execute_script(f"DROP DOMAIN {domain_name}")
        await connection.invalidate_schema_cache()


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
@pytest.mark.parametrize("reader", ["dicts", "described", "stream"])
async def test_anonymous_record_arrays_use_the_common_structured_decoder(db_simple, reader):
    query = (
        "SELECT ARRAY[ROW(1,2),NULL::record,ROW(3,4)] AS records,"
        "ARRAY[]::record[] AS empty,NULL::record[] AS missing,"
        "ARRAY[[ROW(5,6)],[ROW(7,8)]] AS nested"
    )
    async with Transactions.atomic() as connection:
        for _ in range(2):
            if reader == "stream":
                stream = connection.stream(query)
                try:
                    rows = [dict(row) async for row in stream]
                finally:
                    await stream.aclose()
            elif reader == "described":
                result = await connection.execute_described(query)
                rows = [dict(zip(result.columns, row, strict=True)) for row in result.rows]
            else:
                rows = await connection.execute_dicts(query)
            assert len(rows) == 1
            row = rows[0]
            assert [None if value is None else tuple(value) for value in row["records"]] == [(1, 2), None, (3, 4)]
            assert row["empty"] == []
            assert row["missing"] is None
            assert [[tuple(value) for value in values] for values in row["nested"]] == [[(5, 6)], [(7, 8)]]
