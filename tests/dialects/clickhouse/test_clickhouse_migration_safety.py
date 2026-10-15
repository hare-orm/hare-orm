"""The migration safety check on ClickHouse: a table's rows are read off ``system.tables``, so a
small table isn't taken for a large one."""

import pytest

from hare.fields.data.numeric import IntField
from hare.migrations.migration import Migration
from hare.migrations.operations import AlterField
from hare.migrations.safety import MigrationRiskCode, MigrationSafetyChecker
from hare.migrations.state.state import State
from tests.dialects.clickhouse.models import Team


@pytest.mark.asyncio
async def test_a_tables_rows_are_counted(clickhouse_db):
    connection = Team._meta.connection
    rules = connection.dialect.migration_safety_rules
    assert await rules.count_table_rows(connection, "team", None, 10) == 0
    await Team.objects.bulk_create([Team(name=f"team{number}") for number in range(3)])
    assert await rules.count_table_rows(connection, "team", None, 10) == 3
    # A model the database has no table for yet holds no rows.
    assert await rules.count_table_rows(connection, "safety_missing", None, 10) == 0


@pytest.mark.asyncio
async def test_a_table_whose_engine_keeps_no_count_is_large(clickhouse_db):
    connection = Team._meta.connection
    await connection.execute_script("CREATE VIEW safety_view AS SELECT 1 AS id")
    try:
        rules = connection.dialect.migration_safety_rules
        assert await rules.count_table_rows(connection, "safety_view", None, 10) is None
    finally:
        await connection.execute_script("DROP VIEW IF EXISTS safety_view")


@pytest.mark.asyncio
async def test_the_checker_tells_a_small_table_from_a_large_one(clickhouse_db):
    connection = Team._meta.connection
    await Team.objects.bulk_create([Team(name=f"team{number}") for number in range(3)])
    state = State.from_models([Team], app_label="models")
    migration = Migration("0002_change", "models", operations=[AlterField("Team", "name", IntField())])
    for large_table_rows, expected_codes in [(3, [MigrationRiskCode.ALTER_FIELD_REWRITES_TABLE]), (4, [])]:
        risks = await MigrationSafetyChecker(large_table_rows=large_table_rows).check(
            migration, state, dialect=connection.dialect, client=connection
        )
        assert [risk.code for risk in risks] == expected_codes
