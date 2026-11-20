"""Client-local conversions agree across raw statements, transactions and packed membership."""

import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime, time, timedelta, timezone
from decimal import Decimal

import pytest

from hare.core.connections.connections import Connections
from hare.core.hare_context import HareContext
from hare.dialects.sqlite.drivers.aiosqlite.client import AiosqliteClient
from hare.dialects.sqlite.lookups.in_list.sqlite_large_in_list import SqliteLargeInList
from hare.dialects.sqlite.parameters.constants import SQLITE_IN_JSON_ARRAY_THRESHOLD
from hare.dialects.sqlite.parameters.sqlite_parameter_adapters import SqliteParameterAdapters
from hare.exceptions import ConfigurationError
from hare.sql import Table
from hare.sql.terms.parameters.parameterizer import Parameterizer
from tests.shared_models.testmodels import Tournament

pytestmark = pytest.mark.sqlite_only


@pytest.mark.database_independent
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        (True, 1),
        (Decimal("-0.00"), "0.00"),
        (date(2024, 2, 29), "2024-02-29"),
        (datetime(2024, 2, 29, 12, tzinfo=timezone(timedelta(hours=3))), "2024-02-29 09:00:00+00:00"),
        (time(12, 30, tzinfo=UTC), "12:30:00+00:00"),
        (b"\x00\xff", b"\x00\xff"),
    ],
)
def test_local_storage_conversions_leave_global_adapters_unchanged(value, expected):
    previous = dict(sqlite3.adapters)
    with closing(sqlite3.connect(":memory:")) as connection:
        assert connection.execute("SELECT ?", SqliteParameterAdapters().bind([value])).fetchone() == (expected,)
    assert sqlite3.adapters == previous


@pytest.mark.asyncio
async def test_clients_and_transactions_keep_independent_adapter_snapshots():
    clients = [AiosqliteClient(file_path=":memory:", connection_alias=f"adapters_{index}") for index in range(2)]
    previous = dict(sqlite3.adapters)
    try:
        for index, client in enumerate(clients):
            client.register_parameter_adapter(int, lambda value, offset=index: value + offset)
            await client.execute("CREATE TABLE entries (value INTEGER)")
            async with HareContext(), client._in_transaction() as transaction:
                snapshot = transaction.parameter_binding_key
                client.register_parameter_adapter(int, lambda value: value + 10)
                assert transaction.parameter_binding_key is snapshot
                assert client.parameter_binding_key is not snapshot
                await transaction.execute_many("INSERT INTO entries VALUES (?)", [[1], [2]])
                described = await transaction.execute_described("SELECT ? AS value", [1])
                assert described.rows == ((1 + index,),)
                rows = [
                    row[0] async for row in transaction.stream("SELECT ? UNION ALL SELECT ?", [1, 2], chunk_size=1)
                ]
                assert rows == [1 + index, 2 + index]
            result = await client.execute("SELECT value FROM entries ORDER BY value")
            assert [row[0] for row in result.rows] == [1 + index, 2 + index]
            assert (await client.execute("SELECT ?", [1])).rows[0][0] == 11
        assert sqlite3.adapters == previous
    finally:
        for client in clients:
            await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("packed", [False, True])
@pytest.mark.parametrize("composite", [False, True])
async def test_membership_uses_the_same_conversions_as_writes(packed, composite):
    client = AiosqliteClient(file_path=":memory:", connection_alias="membership_adapters")
    client.register_parameter_adapter(int, lambda value: value + 100)
    client.register_parameter_adapter(str, str.upper)
    count = SQLITE_IN_JSON_ARRAY_THRESHOLD if packed else 2
    try:
        await client.execute("CREATE TABLE entries (number INTEGER, label TEXT)")
        await client.execute_many("INSERT INTO entries VALUES (?, ?)", [[1, "first"], [2, "second"]])
        entries = Table("entries")
        membership = SqliteLargeInList(client.parameter_adapters, supports_unhex=client.features.supports_unhex)
        if composite:
            from hare.sql.terms.tuple import Tuple

            values = [(1, "first"), (2, "second")] + [(index + 10, "absent") for index in range(count - 2)]
            condition = membership.row_is_in(Tuple(entries.number, entries.label), values)
        else:
            condition = membership.is_in(entries.label, ["first", "second"] + ["absent"] * (count - 2))
        query = client.query_class.from_(entries).select(entries.number).where(condition).orderby(entries.number)
        parameterizer = Parameterizer()
        sql = query.get_sql(client.query_class.SQL_CONTEXT.copy(parameterizer=parameterizer))
        assert ("json_each" in sql) is packed
        assert [row[0] for row in (await client.execute(sql, parameterizer.values)).rows] == [101, 102]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_replacing_an_adapter_reprepares_cached_packed_membership(db):
    client = Tournament.get_connection()
    previous = client.parameter_adapters
    try:
        client.register_parameter_adapter(str, str.upper)
        await Tournament.objects.create(name="first")
        await Tournament.objects.create(name="second")
        for name in ("first", "second"):
            assert await Tournament.objects.filter(name__in=[name] * SQLITE_IN_JSON_ARRAY_THRESHOLD).values_list(
                "name", flat=True
            ) == [name.upper()]
        client.register_parameter_adapter(str, str.lower)
        assert (
            await Tournament.objects.filter(name__in=["first"] * SQLITE_IN_JSON_ARRAY_THRESHOLD).values_list(
                "name", flat=True
            )
            == []
        )
    finally:
        client.parameter_adapters = previous


@pytest.mark.asyncio
async def test_missing_context_does_not_acquire_transaction_resources(monkeypatch):
    def unavailable():
        raise ConfigurationError("No active context")

    monkeypatch.setattr(Connections, "current", staticmethod(unavailable))
    client = AiosqliteClient(file_path=":memory:", connection_alias="missing_context")
    try:
        with pytest.raises(ConfigurationError, match="No active context"):
            async with client._in_transaction():
                pytest.fail("Transaction entered without a context")
        assert not client._lock.locked()
        assert client._connection is None
    finally:
        await client.close()
