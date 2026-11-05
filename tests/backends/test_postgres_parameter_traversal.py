"""Raw native parameters reject cycles while allowing shared child values."""

import json

import pytest

from hare.contrib.test import requires_features
from hare.dialects.postgresql.fields.ranges import Range


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
@pytest.mark.parametrize("container_type", ["array", "json", "range"])
async def test_cyclic_raw_parameter_is_rejected_before_execution(db_isolated_no_schema, container_type):
    connection = db_isolated_no_schema.get_connection()
    if connection.driver_name != "postgresql":
        pytest.skip("native PostgreSQL parameter traversal")
    if container_type == "array":
        value = []
        value.append(value)
        sql = "SELECT $1::integer[]"
    elif container_type == "json":
        value = {}
        value["child"] = value
        sql = "SELECT $1::jsonb"
    else:
        value = Range(0, 1)
        object.__setattr__(value, "lower", value)
        sql = "SELECT $1::int4range"
    with pytest.raises(ValueError, match="circular reference"):
        await connection.execute(sql, [value])
    assert (await connection.execute_dicts("SELECT 7 AS value"))[0]["value"] == 7


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_raw_parameters_allow_repeated_acyclic_children(db_isolated_no_schema):
    connection = db_isolated_no_schema.get_connection()
    if connection.driver_name != "postgresql":
        pytest.skip("native PostgreSQL parameter traversal")
    shared = [1, 2]
    rows = await connection.execute_dicts(
        "SELECT $1::integer[] AS numbers, $2::jsonb::text AS document",
        [[shared, shared], {"left": shared, "right": shared}],
    )
    assert rows[0]["numbers"] == [[1, 2], [1, 2]]
    assert json.loads(rows[0]["document"]) == {"left": [1, 2], "right": [1, 2]}


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_raw_parameter_nesting_limit_has_a_controlled_error(db_isolated_no_schema):
    connection = db_isolated_no_schema.get_connection()
    if connection.driver_name != "postgresql":
        pytest.skip("native PostgreSQL parameter traversal")
    value = 1
    for _ in range(257):
        value = {"child": value}
    with pytest.raises(RecursionError, match="native nesting depth"):
        await connection.execute("SELECT $1::jsonb", [value])
