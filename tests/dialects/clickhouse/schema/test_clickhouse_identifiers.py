"""Names ClickHouse quotes: a backslash inside one is an escape there, so hare doubles it - a table,
a column and an alias holding one are written and read as they are named."""

import pytest

from hare import Connections
from hare.migrations.drift import detect_drift
from hare.migrations.state.model_state import ModelState
from hare.migrations.state.state import State
from hare.query.expressions import F
from hare.query.functions import Count
from tests.dialects.clickhouse.models import BackslashName, KeywordName, Player, Team


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
    state = State()
    state.models[("models", "BackslashName")] = ModelState.make_from_model("models", BackslashName)
    drift = await detect_drift(BackslashName._meta.connection, state, ["models"])
    assert [
        operation for operation in drift.operations if getattr(operation, "model_name", "") == "BackslashName"
    ] == []


@pytest.mark.asyncio
async def test_drift_on_a_connection_not_yet_open_reads_the_types_of_its_server(clickhouse_db):
    """A JSONField is a JSON column where the server has the type - the connection learns so when it
    opens, before drift compares the first column."""
    state = State()
    state.models[("models", "Player")] = ModelState.make_from_model("models", Player)
    # The model a relation of Player points at - drift builds the models in a shadow.
    state.models[("models", "Team")] = ModelState.make_from_model("models", Team)
    client = Connections.current().create_independent("models", {})
    try:
        drift = await detect_drift(client, state, ["models"])
    finally:
        await client.close()
    assert [mismatch.detail for mismatch in drift.mismatched_columns if mismatch.table == "player"] == []
    assert [operation for operation in drift.operations if getattr(operation, "model_name", "") == "Player"] == []


@pytest.mark.asyncio
async def test_a_key_column_named_from_is_written_and_read(clickhouse_db):
    """The key's uniqueness check selected "from" "0" - a syntax error to ClickHouse."""
    await KeywordName.objects.create(from_field=1, label="a")
    await KeywordName.objects.bulk_create([KeywordName(from_field=2, label="b")])
    assert await KeywordName.objects.order_by("from_field").values_list("from_field", "label") == [(1, "a"), (2, "b")]
    assert (await KeywordName.objects.get(from_field=2)).label == "b"
    assert await KeywordName.objects.annotate(copy=F("from_field")).filter(copy__gt=1).count() == 1
