"""execute_described(): a raw statement's columns - for an empty result too - its rows as tuples in
column order and the rows it changed, on every driver and inside a transaction."""

from __future__ import annotations

import pytest

from hare.contrib.test import requires_features
from hare.exceptions import OperationalError
from hare.transactions.transactions import Transactions
from tests.testmodels import Tournament

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
