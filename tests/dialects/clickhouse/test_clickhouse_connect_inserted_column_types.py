"""A binary insert through clickhouse-connect asks the server for the table's column types once - the
library asks before every insert - and again after a script, or a statement that may change a schema."""

import pytest

pytest.importorskip("clickhouse_connect")

from clickhouse_connect.driver.asyncclient import AsyncClient  # noqa: E402

from hare.core.connections.connections import Connections  # noqa: E402
from hare.dialects.clickhouse.drivers.clickhouse_connect.client.clickhouse_connect_client import (  # noqa: E402
    ClickhouseConnectClient,
)
from tests.dialects.clickhouse.models import Skill  # noqa: E402

PROBE_TABLE_SQL = "CREATE TABLE IF NOT EXISTS hare_column_types_probe (value Int8) ENGINE = Memory"
DROP_PROBE_TABLE_SQL = "DROP TABLE IF EXISTS hare_column_types_probe"


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
    await Skill.objects.bulk_create([Skill(id=1, name="a")])
    try:
        if runs_script:
            await connection.execute_script(PROBE_TABLE_SQL)
        else:
            await connection.execute(PROBE_TABLE_SQL)
        await Skill.objects.bulk_create([Skill(id=2, name="b")])
    finally:
        await connection.execute_script(DROP_PROBE_TABLE_SQL)
    assert asked_column_types == [None, None]


@pytest.mark.parametrize(
    ("sql", "changes_schema"),
    [
        ("ALTER TABLE skill ADD COLUMN extra Int8", True),
        ("  create table t (x Int8) ENGINE = Memory", True),
        ("DROP TABLE t", True),
        ("RENAME TABLE a TO b", True),
        ("EXCHANGE TABLES a AND b", True),
        ("INSERT INTO skill VALUES (1, 'a')", False),
        ("SELECT 1", False),
        ("CREATED", False),
    ],
)
def test_a_statement_changing_a_schema_forgets_the_column_types(sql, changes_schema):
    ClickhouseConnectClient.inserted_column_types[("host", 1, "database", "skill", ("id",))] = []
    ClickhouseConnectClient.forget_inserted_column_types(sql)
    assert (len(ClickhouseConnectClient.inserted_column_types) == 0) is changes_schema
    ClickhouseConnectClient.inserted_column_types.clear()
