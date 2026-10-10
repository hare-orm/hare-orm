"""Lightweight ``UPDATE`` on ClickHouse 25.7: a table declaring ``lightweight_updates`` is changed by
them (``update()``, ``save()``, ``bulk_update()``); the option is turned on and off in place, and
``hare drift`` reads it back."""

import pytest

from hare import fields
from hare.dialects.clickhouse.clickhouse_table_options import ClickhouseTableOptions
from hare.migrations.drift import detect_drift
from hare.migrations.operations import AlterModelOptions, CreateModel, DeleteModel
from hare.migrations.state.model_state import ModelState
from hare.migrations.state.state import State
from hare.migrations.state.state_apps import StateApps
from tests.dialects.clickhouse.lightweight_updates.models import Ledger


async def get_update_statements(connection, table):
    await connection.execute_script("SYSTEM FLUSH LOGS")
    rows = await connection.execute_dicts(
        "SELECT query FROM system.query_log WHERE type = 'QueryFinish' AND current_database = currentDatabase() "
        f"AND (query ILIKE 'UPDATE \"{table}\"%' OR query ILIKE 'ALTER TABLE \"{table}\" UPDATE%') "
        "ORDER BY event_time_microseconds"
    )
    return [row["query"].split(" SET ")[0].split(" UPDATE ")[0] for row in rows]


@pytest.mark.asyncio
async def test_a_table_declaring_lightweight_updates_is_changed_by_them(clickhouse_lightweight_updates_db):
    connection = Ledger._meta.connection
    await Ledger.objects.bulk_create(
        [Ledger(id=number, owner="ab"[number % 2], balance=number) for number in range(6)]
    )
    assert await Ledger.objects.filter(owner="a").update(balance=100) == 3
    ledger = await Ledger.objects.get(id=1)
    ledger.balance = 7
    await ledger.save()
    ledgers = await Ledger.objects.filter(id__in=[3, 5]).order_by("id")
    for ledger in ledgers:
        ledger.balance = -ledger.id
    assert await Ledger.objects.bulk_update(ledgers, ["balance"]) == 2
    # Read at once - the new values stand in for the old ones.
    assert await Ledger.objects.order_by("id").values_list("balance", flat=True) == [100, 7, 100, -3, 100, -5]
    statements = await get_update_statements(connection, "ledger")
    assert statements and all(statement.startswith('UPDATE "ledger"') for statement in statements)
    (table,) = await connection.execute_dicts(
        "SELECT engine_full FROM system.tables WHERE database = currentDatabase() AND name = 'ledger'"
    )
    assert "enable_block_number_column = 1, enable_block_offset_column = 1" in table["engine_full"]
    # A key keeps its values.
    with pytest.raises(Exception, match="can not be updated"):
        await Ledger.objects.filter(id=1).update(id=10)


@pytest.mark.asyncio
async def test_lightweight_updates_are_turned_on_and_off_in_place(clickhouse_lightweight_updates_db):
    connection = Ledger._meta.connection
    editor = connection.dialect.schema_editor_class(connection, atomic=True, collect_sql=False)
    state = State(models={}, apps=StateApps())
    await CreateModel(
        name="Wallet",
        fields=[("id", fields.BigIntField(primary_key=True, generated=False)), ("coins", fields.IntField())],
        options={"table": "wallet"},
    ).run("models", state, dry_run=False, state_editor=editor)

    async def get_table():
        (row,) = await connection.execute_dicts(
            "SELECT toString(uuid) AS uuid, engine_full FROM system.tables "
            "WHERE database = currentDatabase() AND name = 'wallet'"
        )
        return row["uuid"], "enable_block_number_column = 1" in row["engine_full"]

    try:
        uuid, enabled = await get_table()
        assert not enabled
        for lightweight_updates in (True, False):
            await AlterModelOptions(
                name="Wallet",
                options={
                    "table": "wallet",
                    "table_options": [ClickhouseTableOptions(lightweight_updates=lightweight_updates)],
                },
            ).run("models", state, dry_run=False, state_editor=editor)
            # The same table - nothing was copied.
            assert await get_table() == (uuid, lightweight_updates)
    finally:
        await DeleteModel(name="Wallet").run("models", state, dry_run=False, state_editor=editor)


@pytest.mark.asyncio
async def test_drift_reads_the_option_back(clickhouse_lightweight_updates_db):
    connection = Ledger._meta.connection
    state = State(models={}, apps=StateApps())
    state.models[("models", "Ledger")] = ModelState.make_from_model("models", Ledger)
    drift = await detect_drift(connection, state, ["models"])
    assert [operation for operation in drift.operations if isinstance(operation, AlterModelOptions)] == []
    await connection.execute_script(
        "ALTER TABLE ledger RESET SETTING enable_block_number_column, enable_block_offset_column"
    )
    try:
        drift = await detect_drift(connection, state, ["models"])
        (operation,) = [operation for operation in drift.operations if isinstance(operation, AlterModelOptions)]
        # The operation bringing the table back to the declaration.
        assert operation.options["table_options"] == (ClickhouseTableOptions(lightweight_updates=True),)
    finally:
        await connection.execute_script(
            "ALTER TABLE ledger MODIFY SETTING enable_block_number_column = 1, enable_block_offset_column = 1"
        )
