"""A migration changing the type of a PostgreSQL range column: PostgreSQL casts no range type to
another, so the column is converted through the value's text."""

from __future__ import annotations

from typing import Any

import pytest

from hare import Connections, fields
from hare.contrib.test import requires_features
from hare.dialects.postgresql.fields.multiranges import BigIntMultiRangeField, IntMultiRangeField
from hare.dialects.postgresql.fields.ranges import BigIntRangeField, DecimalRangeField, IntRangeField
from hare.migrations.autodetection.operation_generator import OperationGenerator
from hare.migrations.migration import Migration
from hare.migrations.operations import CreateModel
from hare.migrations.state.state import State

TABLE = "range_migration_booking"


def get_state(**state_fields: Any) -> State:
    state = State()
    CreateModel(
        name="Booking",
        fields=[("id", fields.IntField(primary_key=True)), *state_fields.items()],
        options={"table": TABLE, "app": "models"},
    ).state_forward("models", state)
    return state


def make_migration(name: str, *operations) -> Migration:
    return Migration(name=name, app_label="models", operations=list(operations))


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_a_range_column_changes_its_range_type(db_simple):
    connection = Connections.get("models")

    def get_editor():
        return connection.dialect.schema_editor_class(connection, atomic=True, collect_sql=False)

    async def get_values() -> list[tuple[str, str, str]]:
        rows = await connection.execute_dicts(
            f"SELECT span::text AS span, spans::text AS spans, history::text AS history FROM {TABLE} ORDER BY id"
        )
        return [(row["span"], row["spans"], row["history"]) for row in rows]

    async def get_types() -> list[str]:
        rows = await connection.execute_dicts(
            f"SELECT udt_name FROM information_schema.columns WHERE table_name = '{TABLE}' "
            "AND column_name IN ('span', 'spans', 'history') ORDER BY column_name"
        )
        return [row["udt_name"] for row in rows]

    empty = State()
    narrow = get_state(
        span=IntRangeField(null=True),
        spans=IntMultiRangeField(null=True),
        history=fields.ArrayField(IntRangeField(), null=True),
    )
    wide = get_state(
        span=BigIntRangeField(null=True),
        spans=BigIntMultiRangeField(null=True),
        history=fields.ArrayField(BigIntRangeField(), null=True),
    )
    decimal = get_state(
        span=DecimalRangeField(null=True),
        spans=BigIntMultiRangeField(null=True),
        history=fields.ArrayField(BigIntRangeField(), null=True),
    )
    initial = make_migration("0001_initial", *OperationGenerator(empty, narrow).generate())
    state = await initial.apply(empty.clone(), schema_editor=get_editor())
    try:
        await connection.execute_script(
            f"INSERT INTO {TABLE} (id, span, spans, history) VALUES "
            "(1, '[1,5)', '{[1,3), [7,9)}', ARRAY['[2,4)'::int4range, 'empty'::int4range]), (2, 'empty', '{}', NULL)"
        )
        values = [("[1,5)", "{[1,3),[7,9)}", '{"[2,4)",empty}'), ("empty", "{}", None)]
        widen = make_migration("0002_widen", *OperationGenerator(narrow, wide).generate())
        state = await widen.apply(state, schema_editor=get_editor())
        assert await get_types() == ["_int8range", "int8range", "int8multirange"]
        assert await get_values() == values
        to_decimal = make_migration("0003_decimal", *OperationGenerator(wide, decimal).generate())
        await to_decimal.apply(state.clone(), schema_editor=get_editor())
        assert await get_values() == values
        await to_decimal.unapply(state, schema_editor=get_editor())
        await widen.unapply(narrow, schema_editor=get_editor())
        assert await get_types() == ["_int4range", "int4range", "int4multirange"]
        assert await get_values() == values
    finally:
        await connection.execute_script(f"DROP TABLE IF EXISTS {TABLE}")
