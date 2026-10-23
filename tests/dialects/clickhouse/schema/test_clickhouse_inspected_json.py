"""inspectdb of a ClickHouse JSON column: a JSONField is a JSON column on a connection whose driver reads
one - the column's type was compared with the type of the dialect's default variant, a String, and
the field got a note of a type that differs."""

import importlib

import pytest

from hare.inspectdb import SchemaInspector
from tests.dialects.clickhouse.models import Player


@pytest.mark.asyncio
async def test_a_json_column_is_a_json_field_of_its_type_where_the_driver_reads_one(clickhouse_db):
    connection = Player._meta.connection
    await connection.execute_script("CREATE TABLE inspected_json (id Int32, data JSON) ENGINE = MergeTree ORDER BY id")
    try:
        source = await SchemaInspector.inspect(connection, tables=["inspected_json"])
    finally:
        await connection.execute_script("DROP TABLE inspected_json")
    (line,) = [line for line in source.splitlines() if "data = " in line]
    assert line.strip().startswith("data = fields.JSONField()")
    assert ("TODO" in line) is not connection.features.supports_json_type


@pytest.mark.database_independent
@pytest.mark.parametrize(
    ("driver_package", "client_class_name", "stores_json_natively"),
    [("clickhouse_connect", "ClickhouseConnectClient", True), ("clickhouse_driver", "ClickhouseDriverClient", False)],
)
def test_a_client_picks_its_json_column_before_it_connects(driver_package, client_class_name, stores_json_natively):
    """The dialect was picked on connect, so sqlmigrate - which never connects - printed a String
    column where migrate created a JSON one."""
    pytest.importorskip(driver_package)
    client_module = importlib.import_module(
        f"hare.dialects.clickhouse.drivers.{driver_package}.client.{driver_package}_client"
    )
    client = getattr(client_module, client_class_name)(connection_alias="unconnected", database="unconnected")
    assert client.dialect.stores_json_natively is stores_json_natively
    assert client.features.supports_json_type is stores_json_natively
