"""A binary insert through clickhouse-connect asks the server for the table's column types once - the
library asks before every insert - and again at an explicit schema boundary."""

import asyncio
import datetime
import logging
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from clickhouse_connect.datatypes.registry import get_from_name
from clickhouse_connect.driver.asyncclient import AsyncClient  # noqa: E402

from hare.core.connections.connections import Connections  # noqa: E402
from hare.dialects.clickhouse.drivers.clickhouse_connect.client.clickhouse_connect_client import (  # noqa: E402
    ClickhouseConnectClient,
)
from hare.dialects.clickhouse.drivers.clickhouse_connect.client.clickhouse_connect_transaction_client import (
    ClickhouseConnectTransactionClient,
)
from hare.dialects.clickhouse.drivers.clickhouse_connect.client.clickhouse_connect_transform import (
    ClickhouseConnectTransform,
)
from tests.dialects.clickhouse.models import Skill  # noqa: E402

PROBE_TABLE_SQL = "CREATE TABLE IF NOT EXISTS hare_column_types_probe (value Int8) ENGINE = Memory"
DROP_PROBE_TABLE_SQL = "DROP TABLE IF EXISTS hare_column_types_probe"


@pytest.mark.database_independent
@pytest.mark.asyncio
@pytest.mark.parametrize("transaction", (False, True))
@pytest.mark.parametrize("outcome", ("cold", "warm", "prepare_error", "insert_error", "cancel"))
async def test_copy_prepares_local_codecs_before_data_and_commits_types_on_success(monkeypatch, transaction, outcome):
    client_class = ClickhouseConnectTransactionClient if transaction else ClickhouseConnectClient
    client = object.__new__(client_class)
    client.log = logging.getLogger(__name__)
    client.host, client.port, client.database = "host", 8123, "database"
    columns = ["moment", "payload"]
    type_names = ["DateTime64(6, 'UTC')", "Dynamic"]
    declared_types = [get_from_name(name) for name in type_names]
    key = (client.host, client.port, client.database, "events", tuple(columns))
    cache = {} if outcome == "cold" else {key: declared_types}
    monkeypatch.setattr(ClickhouseConnectClient, "inserted_column_types", cache)
    tick_converter = Mock(return_value=[17])
    monkeypatch.setattr(ClickhouseConnectClient, "native_rows", SimpleNamespace(get_moment_ticks=tick_converter))
    catalog = AsyncMock(return_value=[{"name": name, "type": value} for name, value in zip(columns, type_names)])
    client._parent = SimpleNamespace(execute_dicts=catalog)
    transform = ClickhouseConnectTransform()
    events = []
    factory_arguments = []
    library = SimpleNamespace(server_tz=datetime.UTC, _execute_operation=AsyncMock(side_effect=AssertionError("SQL")))

    async def create_context(table, column_names, **kwargs):
        factory_arguments.append(kwargs.copy())
        if kwargs.get("column_types") is None and "column_type_names" not in kwargs:
            kwargs["column_types"] = declared_types  # The normal client's DESCRIBE result.
        return await AsyncClient.create_insert_context(library, table, column_names, **kwargs)

    def prepare(context):
        assert context.empty
        events.append("prepare")
        if outcome == "prepare_error":
            raise RuntimeError("preparation failed")
        transform.prepare_insert(context)

    async def insert(context):
        events.append("insert")
        assert context.data == [[17], ['{"value":7}']]
        assert context.column_types[1] is not declared_types[1]
        if outcome == "insert_error":
            raise RuntimeError("insert failed")
        if outcome == "cancel":
            raise asyncio.CancelledError

    library.create_insert_context = create_context
    library._transform = SimpleNamespace(prepare_insert=prepare)
    library.data_insert = insert

    @asynccontextmanager
    async def acquire():
        events.append("acquire")
        try:
            yield library
        finally:
            events.append("release")

    client.acquire_connection = acquire
    records = [(datetime.datetime(2024, 1, 2, tzinfo=datetime.UTC), {"value": 7})]
    operation = client.copy.__wrapped__(client, "events", columns, records, type_names)
    if outcome == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await operation
    elif outcome.endswith("error"):
        with pytest.raises(RuntimeError):
            await operation
    else:
        await operation
    assert events == ["acquire", "prepare", *([] if outcome == "prepare_error" else ["insert"]), "release"]
    assert tick_converter.call_count == (0 if outcome == "prepare_error" else 1)
    library._execute_operation.assert_not_awaited()
    if transaction:
        catalog.assert_awaited_once()
        assert factory_arguments == [{"column_type_names": type_names, "column_oriented": True}]
        assert cache == ({} if outcome == "cold" else {key: declared_types})
    else:
        catalog.assert_not_awaited()
        assert factory_arguments == [
            {"column_types": None if outcome == "cold" else declared_types, "column_oriented": True}
        ]
        assert cache == ({key: declared_types} if outcome in {"cold", "warm"} else {})


@pytest.fixture
def asked_column_types(clickhouse_db, monkeypatch):
    """The column types each insert hands the library - None where the library asks the server."""
    if not isinstance(Connections.get("models"), ClickhouseConnectClient):
        pytest.skip("clickhouse-connect alone asks the server before an insert")
    ClickhouseConnectClient.inserted_column_types.clear()
    passed = []
    library_create_insert_context = AsyncClient.create_insert_context

    async def recording_create_insert_context(self, table, column_names=None, *args, **kwargs):
        passed.append(kwargs.get("column_types"))
        return await library_create_insert_context(self, table, column_names, *args, **kwargs)

    monkeypatch.setattr(AsyncClient, "create_insert_context", recording_create_insert_context)
    return passed


@pytest.mark.asyncio
async def test_the_column_types_are_asked_once_per_table(asked_column_types):
    await Skill.objects.bulk_create([Skill(id=1, name="a")])
    await Skill.objects.bulk_create([Skill(id=2, name="b")])
    assert asked_column_types[0] is None
    assert asked_column_types[1] is not None
    assert sorted(await Skill.objects.values_list("name", flat=True)) == ["a", "b"]


@pytest.mark.asyncio
@pytest.mark.parametrize("runs_script", [True, False])
async def test_the_column_types_are_asked_again_after_a_schema_change(asked_column_types, runs_script):
    connection = Connections.get("models")
    editor = connection.dialect.schema_editor_class(connection)
    await Skill.objects.bulk_create([Skill(id=1, name="a")])
    try:
        if runs_script:
            await editor.run_raw_sql(PROBE_TABLE_SQL)
        else:
            await editor.run_parameterized_sql(PROBE_TABLE_SQL, [], schema_change=True)
        await Skill.objects.bulk_create([Skill(id=2, name="b")])
    finally:
        await editor.run_raw_sql(DROP_PROBE_TABLE_SQL)
    assert asked_column_types == [None, None]


@pytest.mark.database_independent
@pytest.mark.asyncio
@pytest.mark.parametrize("placement_fails", [False, True])
async def test_schema_refresh_forgets_insert_types_even_if_placement_refresh_fails(placement_fails):
    client = object.__new__(ClickhouseConnectClient)
    refresh = AsyncMock(side_effect=RuntimeError("placement failed") if placement_fails else None)
    client.cluster = SimpleNamespace(refresh_databases=refresh)
    ClickhouseConnectClient.inserted_column_types[("host", 1, "database", "skill", ("id",))] = []
    try:
        if placement_fails:
            with pytest.raises(RuntimeError, match="placement failed"):
                await client.invalidate_schema_cache()
        else:
            await client.invalidate_schema_cache()
        assert not ClickhouseConnectClient.inserted_column_types
        refresh.assert_awaited_once()
    finally:
        ClickhouseConnectClient.inserted_column_types.clear()
