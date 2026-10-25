"""A JSON column read over clickhouse-driver - a table made over clickhouse-connect: the session escapes a dot
of a key (json_type_escape_dots_in_keys), which came back as %2E instead of the dot."""

import pytest

from tests.dialects.clickhouse.models import Player


@pytest.mark.asyncio
async def test_a_key_holding_a_dot_is_read_with_the_dot(clickhouse_db):
    connection = Player._meta.connection
    if connection.features.supports_json_type:
        pytest.skip("clickhouse-driver reads the JSON column of a table made over clickhouse-connect")
    await connection.execute_script("DROP TABLE IF EXISTS json_over_driver")
    try:
        await connection.execute_script(
            "CREATE TABLE json_over_driver (id Int32, data Nullable(JSON), text String) ENGINE = MergeTree ORDER BY id"
        )
        await connection.execute_script(
            "INSERT INTO json_over_driver VALUES "
            '(1, \'{"a%2Eb": 2, "c": {"d%2Ee": [{"f%2Eg": 1}]}, "s": "x%2Ey"}\', \'{"x%2Ey": 1}\'), '
            "(2, NULL, '')"
        )

        rows = await connection.execute_dicts("SELECT id, data, text FROM json_over_driver ORDER BY id")

        assert rows == [
            {"id": 1, "data": {"a.b": 2, "c": {"d.e": [{"f.g": 1}]}, "s": "x%2Ey"}, "text": '{"x%2Ey": 1}'},
            {"id": 2, "data": None, "text": ""},
        ]
    finally:
        await connection.execute_script("DROP TABLE IF EXISTS json_over_driver")
