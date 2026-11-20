"""execute_described(): a raw statement's columns - for an empty result too - its rows as tuples in
column order and the rows it changed, on every driver and inside a transaction."""

from __future__ import annotations

from contextlib import nullcontext
from uuid import UUID

import pytest

from hare.contrib.test import requires_features
from hare.core.connections.connections import Connections
from hare.exceptions import OperationalError
from hare.transactions.transactions import Transactions
from tests.shared_models.testmodels import Tournament, UUIDFields

CONNECTION = "models"


async def create_tournaments() -> None:
    await Tournament.objects.create(id=1, name="Spring")
    await Tournament.objects.create(id=2, name="Autumn")


@pytest.mark.asyncio
async def test_columns_rows_and_row_count(db):
    await create_tournaments()
    connection = db.get_connection(CONNECTION)

    result = await connection.execute_described('SELECT "id", "name" FROM "tournament" ORDER BY "id"')
    assert result.columns == ("id", "name")
    assert result.rows == ((1, "Spring"), (2, "Autumn"))
    assert result.row_count == 2

    empty = await connection.execute_described('SELECT "id", "name" FROM "tournament" WHERE "id" < 0')
    assert empty.columns == ("id", "name")
    assert empty.rows == ()
    assert empty.row_count == 0

    same_names = await connection.execute_described("SELECT 1 AS value, 2 AS value")
    assert same_names.columns == ("value", "value")
    assert same_names.rows == ((1, 2),)


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_raw_and_model_reads_preserve_uuid_and_null_from_the_same_row(db):
    await UUIDFields.objects.create(id=UUID(int=12), data=UUID(int=34), data_null=None)
    connection = db.get_connection(CONNECTION)
    result = await connection.execute_described('SELECT "data_null", "data", "id" FROM "uuidfields"')
    assert result.columns == ("data_null", "data", "id")
    assert result.rows == ((None, UUID(int=34), UUID(int=12)),)
    assert isinstance(result.rows[0][1], UUID)
    rows = await UUIDFields.objects.values_list("data_null", "data", "id")
    assert rows == [(None, UUID(int=34), UUID(int=12))]
    assert type(rows[0][1]) is UUID


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
@pytest.mark.parametrize("cache_size", [0, 8], ids=["uncached", "cached"])
@pytest.mark.parametrize("in_transaction", [False, True], ids=["pool", "transaction"])
async def test_a_wrong_raw_parameter_count_leaves_the_connection_usable(db_simple, cache_size, in_transaction):
    client = Connections.current().create_independent(CONNECTION, {"statement_cache_size": cache_size})
    sql = "SELECT $1::uuid AS value WHERE $1 = $1 /* $2 */"
    value = UUID(int=34)
    try:
        async with client._in_transaction() if in_transaction else nullcontext(client) as connection:
            assert (await connection.execute_described(sql, [value])).rows == ((value,),)
            with pytest.raises(OperationalError):
                await connection.execute_described(sql, [])
            with pytest.raises(OperationalError):
                await connection.execute(sql, [value, UUID(int=56)], returns_rows=True)
            with pytest.raises(OperationalError):
                await connection.execute("SELECT $tag$$1$tag$", [value], returns_rows=False)
            with pytest.raises(OperationalError):
                await connection.execute_many(sql, [[value], [value, UUID(int=56)]])
            if in_transaction:
                with pytest.raises(OperationalError):
                    _ = [row async for row in connection.stream(sql, [value, UUID(int=56)])]
                assert [row["value"] async for row in connection.stream(sql, [value])] == [value]
            assert await connection.execute_dicts(sql, [value]) == [{"value": value}]
    finally:
        await client.close()


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_raw_parameter_types_come_from_sql(db_simple):
    connection = db_simple.get_connection(CONNECTION)
    value = UUID(int=34)
    assert await connection.execute_dicts("SELECT pg_typeof($1::uuid)::text AS kind", [value]) == [{"kind": "uuid"}]
    with pytest.raises(OperationalError):
        await connection.execute_described("SELECT pg_typeof($1)::text", [value])


@pytest.mark.asyncio
async def test_writes_report_the_rows_they_changed(db):
    await create_tournaments()
    connection = db.get_connection(CONNECTION)

    update = await connection.execute_described('UPDATE "tournament" SET "desc" = \'x\'')
    assert (update.columns, update.rows, update.row_count) == ((), (), 2)

    returning = await connection.execute_described(
        'UPDATE "tournament" SET "desc" = \'y\' WHERE "id" = 1 RETURNING id'
    )
    assert (returning.columns, returning.rows, returning.row_count) == (("id",), ((1,),), 1)


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_inside_a_transaction_and_with_parameters(db):
    await create_tournaments()
    placeholder = db.get_connection(CONNECTION).dialect.parameters.get_placeholder(1)
    async with Transactions.atomic(CONNECTION) as transaction:
        result = await transaction.execute_described(
            f'SELECT "name" FROM "tournament" WHERE "id" = {placeholder}', [2]
        )
        empty = await transaction.execute_described(f'SELECT "name" FROM "tournament" WHERE "id" = {placeholder}', [9])
    assert (result.columns, result.rows) == (("name",), (("Autumn",),))
    assert (empty.columns, empty.rows) == (("name",), ())


@pytest.mark.asyncio
async def test_a_wrong_statement_raises(db):
    with pytest.raises(OperationalError):
        await db.get_connection(CONNECTION).execute_described('SELECT "missing" FROM "tournament"')
