"""inspectdb of ClickHouse schema objects no model is made of: a dictionary was skipped without a note,
and an EPHEMERAL column came back as a plain field - a migration of the model then stored it."""

import pytest

from hare.inspectdb import SchemaInspector, SchemaNotFoundError
from tests.dialects.clickhouse.models import Player


@pytest.mark.asyncio
async def test_a_dictionary_is_named_and_an_ephemeral_column_marked(clickhouse_db):
    connection = Player._meta.connection
    await connection.execute_script(
        "CREATE TABLE inspected_source (id UInt64, label String, raw UInt32 EPHEMERAL, size UInt32 DEFAULT raw * 2) "
        "ENGINE = MergeTree ORDER BY id;"
        "CREATE DICTIONARY inspected_labels (id UInt64, label String) PRIMARY KEY id "
        "SOURCE(CLICKHOUSE(TABLE 'inspected_source')) LAYOUT(FLAT()) LIFETIME(300)"
    )
    try:
        source = await SchemaInspector.inspect(connection)
    finally:
        await connection.execute_script("DROP DICTIONARY inspected_labels; DROP TABLE inspected_source")
    (line,) = [line for line in source.splitlines() if line.strip().startswith("raw = ")]
    assert "TODO: an EPHEMERAL column" in line
    assert "# NOTE: dictionary 'inspected_labels' isn't a model" in source


@pytest.mark.asyncio
async def test_enums_of_one_name_in_two_tables_are_declared_apart(clickhouse_db):
    """Both were declared "VLabels" - the second declaration replaced the first, and the first model's
    field silently took the other table's members."""
    connection = Player._meta.connection
    await connection.execute_script(
        "CREATE TABLE inspected_colors (id Int32, v Enum8('red' = 1, 'blue' = 2)) ENGINE = MergeTree ORDER BY id;"
        "CREATE TABLE inspected_sizes (id Int32, v Enum8('s' = 1, 'l' = 2)) ENGINE = MergeTree ORDER BY id"
    )
    try:
        source = await SchemaInspector.inspect(connection, ["inspected_colors", "inspected_sizes"])
    finally:
        await connection.execute_script("DROP TABLE inspected_colors; DROP TABLE inspected_sizes")
    namespace: dict = {}
    exec(source, namespace)  # noqa: S102 - the source inspectdb generated
    colors, sizes = namespace["InspectedColors"], namespace["InspectedSizes"]
    assert [member.name for member in colors._meta.fields_map["v"].enum_type] == ["red", "blue"]
    assert [member.name for member in sizes._meta.fields_map["v"].enum_type] == ["s", "l"]


@pytest.mark.asyncio
async def test_a_database_that_does_not_exist_is_refused(clickhouse_db):
    """It was read as a database without tables - a typo in its name inspected and drifted as nothing."""
    with pytest.raises(SchemaNotFoundError):
        await SchemaInspector.inspect(Player._meta.connection, schema="no_such_inspected_database")
