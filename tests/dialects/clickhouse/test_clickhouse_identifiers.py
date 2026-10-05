"""Names ClickHouse quotes: a backslash inside one is an escape there, so hare doubles it - a table,
a column and an alias holding one are written and read as they are named."""

import pytest

from hare import Connections
from hare.migrations.drift import detect_drift
from hare.migrations.state.model_state import ModelState
from hare.migrations.state.state import State
from hare.migrations.state.state_apps import StateApps
from hare.query.expressions import F
from hare.query.functions import Count
from tests.dialects.clickhouse.models import BackslashName, Player


@pytest.mark.asyncio
async def test_a_name_holding_a_backslash_is_written_and_read(clickhouse_db):
    await BackslashName.objects.bulk_create([BackslashName(id=1, label="a"), BackslashName(id=2, label="b")])
    assert await BackslashName.objects.filter(label="b").values_list("id", flat=True) == [2]
    assert await BackslashName.objects.annotate(**{"copy\\": F("label")}).order_by("id").values_list(
        "copy\\", flat=True
    ) == ["a", "b"]
    assert await BackslashName.objects.annotate(**{"n\\": Count("id")}).values("n\\") == [{"n\\": 1}, {"n\\": 1}]
    rows = await BackslashName._meta.connection.execute_dicts(
        "SELECT name FROM system.tables WHERE database = currentDatabase() AND name LIKE 'back%'"
    )
    assert [row["name"] for row in rows] == ["back\\slash"]


@pytest.mark.asyncio
async def test_a_name_holding_a_backslash_shows_no_drift(clickhouse_db):
    state = State(models={}, apps=StateApps())
    state.models[("models", "BackslashName")] = ModelState.make_from_model("models", BackslashName)
    drift = await detect_drift(BackslashName._meta.connection, state, ["models"])
    assert [
        operation for operation in drift.operations if getattr(operation, "model_name", "") == "BackslashName"
    ] == []


@pytest.mark.asyncio
async def test_drift_on_a_connection_not_yet_open_reads_the_types_of_its_server(clickhouse_db):
    """A JSONField is a JSON column where the server has the type - the connection learns so when it
    opens, before drift compares the first column."""
    state = State(models={}, apps=StateApps())
    state.models[("models", "Player")] = ModelState.make_from_model("models", Player)
    client = Connections.current().create_independent("models", {})
    try:
        drift = await detect_drift(client, state, ["models"])
    finally:
        await client.close()
    assert [mismatch.detail for mismatch in drift.mismatched_columns if mismatch.table == "player"] == []
    assert [operation for operation in drift.operations if getattr(operation, "model_name", "") == "Player"] == []
