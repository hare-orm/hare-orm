"""The keys, the time to live, the projections and a materialized view's storage of a ClickHouse table
declared by expressions of hare and querysets: written in ClickHouse's SQL, read back as created (no drift,
no second migration), and renamed with the fields they read - a renamed key column keeps its rows."""

import os
import textwrap
from datetime import timedelta

import pytest
import pytest_asyncio

from hare import fields
from hare.ddl.raw_sql_term import RawSQLTerm
from hare.dialects.clickhouse.clickhouse_table_options import ClickhouseTableOptions
from hare.dialects.clickhouse.schema_objects.clickhouse_materialized_view import ClickhouseMaterializedView
from hare.migrations.drift import detect_drift
from hare.query.expressions import F
from hare.query.functions import Trunc
from tests.migrations.round_trips.test_round_trip_real_db import APP_LABEL, RoundTrip, build_live_state, build_model
from tests.utils.cli_project_factory import project_factory  # noqa: F401


@pytest_asyncio.fixture
async def round_trip(clickhouse_db):
    """Migrations of the test's own models on the ClickHouse test server - their tables dropped after it."""
    round_trip = RoundTrip(clickhouse_db.connections.get("models"))
    try:
        yield round_trip
    finally:
        await round_trip.migrate_to()


def build_visit(moment_field_name: str):
    return build_model(
        "Visit",
        "structural_visit",
        {moment_field_name: fields.DatetimeField(), "site": fields.CharField(max_length=20)},
        {
            "table_options": [
                ClickhouseTableOptions(
                    order_by=("id", Trunc(moment_field_name, "day")),
                    partition_by=Trunc(moment_field_name, "month"),
                    ttl=F(moment_field_name) + timedelta(days=36500),
                    column_ttls=(("site", F(moment_field_name) + timedelta(days=36500)),),
                )
            ]
        },
    )


async def get_table_keys(connection, table: str) -> dict[str, str]:
    (row,) = await connection.execute_dicts(
        "SELECT sorting_key, partition_key, create_table_query FROM system.tables "
        f"WHERE database = currentDatabase() AND name = '{table}'"
    )
    return row


@pytest.mark.asyncio
async def test_keys_and_times_to_live_of_expressions_of_hare_follow_a_rename(round_trip):
    connection = round_trip.connection
    visit = build_visit("visited_at")
    await round_trip.migrate_to(visit)
    keys = await get_table_keys(connection, "structural_visit")
    assert "visited_at" in keys["sorting_key"] and "visited_at" in keys["partition_key"]
    assert (await detect_drift(connection, build_live_state(visit), [APP_LABEL])).operations == []
    assert round_trip.get_pending_operations(visit) == []
    assert ClickhouseTableOptions.get_of_dialect(visit._meta.table_options, connection.dialect).get_fixed_field_names(
        visit
    ) == frozenset({"visited_at"})
    await connection.execute_script(
        "INSERT INTO structural_visit (id, visited_at, site) VALUES (1, '2026-01-02 03:04:05', 'a')"
    )

    renamed_visit = build_visit("seen")
    operations = await round_trip.migrate_to(renamed_visit, renames={"models.Visit.visited_at": "seen"})

    assert [type(operation).__name__ for operation in operations] == ["RenameField"]
    assert round_trip.get_pending_operations(renamed_visit) == []
    renamed_keys = await get_table_keys(connection, "structural_visit")
    assert "seen" in renamed_keys["sorting_key"] and "seen" in renamed_keys["partition_key"]
    # As text - the drivers read a moment with a time zone and without one.
    assert await connection.execute_dicts("SELECT id, toString(seen) AS seen, site FROM structural_visit") == [
        {"id": 1, "seen": "2026-01-02 03:04:05.000000", "site": "a"}
    ]


def skip_without_clickhouse() -> None:
    if not os.getenv("HARE_TEST_DB", "").startswith("clickhouse"):
        pytest.skip("runs against the ClickHouse test server - make test_clickhouse")


PROJECTION_MODELS_TEMPLATE = """
from hare.dialects.clickhouse.clickhouse_table_options import ClickhouseTableOptions
from hare.dialects.clickhouse.schema_objects.clickhouse_projection import ClickhouseProjection
from hare.query.functions import Sum
from hare.query.queryset import ModelQuery

class Hit(Model):
    id = fields.BigIntField(primary_key=True, generated=False)
    site = fields.CharField(max_length=20)
    {duration} = fields.IntField()
    class Meta:
        table_options = [
            ClickhouseTableOptions(
                projections=(ClickhouseProjection("by_site", {query}),)
            )
        ]
"""


@pytest.mark.asyncio
async def test_a_projection_of_a_queryset_of_the_model(project_factory):  # noqa: F811
    skip_without_clickhouse()
    project = await project_factory()
    query = 'ModelQuery().values("site").annotate(total=Sum("{duration}"))'
    project.write_models(
        textwrap.dedent(
            PROJECTION_MODELS_TEMPLATE.format(duration="duration", query=query.format(duration="duration"))
        )
    )
    await project.cli_ok("makemigrations")
    assert 'read_columns=("duration", "site")' in project.read_latest_migration()
    await project.cli_ok("migrate")
    await project.assert_in_sync()
    (row,) = await project.query(
        "SELECT create_table_query FROM system.tables WHERE database = currentDatabase() AND name = 'hit'"
    )
    assert "PROJECTION by_site" in row["create_table_query"]

    project.write_models(
        textwrap.dedent(PROJECTION_MODELS_TEMPLATE.format(duration="length", query=query.format(duration="length")))
    )
    await project.cli_ok("makemigrations", "--rename", "app.Hit.duration=length")
    await project.cli_ok("migrate")
    await project.assert_in_sync()
    (row,) = await project.query(
        "SELECT create_table_query FROM system.tables WHERE database = currentDatabase() AND name = 'hit'"
    )
    assert "length" in row["create_table_query"]
    await project.cli_ok("migrate", "app", "zero")


@pytest.mark.asyncio
async def test_a_projection_keeps_every_row(project_factory):  # noqa: F811
    skip_without_clickhouse()
    project = await project_factory()
    query = 'ModelQuery().filter(site="a").values("site")'
    project.write_models(textwrap.dedent(PROJECTION_MODELS_TEMPLATE.format(duration="duration", query=query)))
    refused = await project.cli("makemigrations")
    assert refused.exit_code not in (0, None)
    assert "filters, joins or limits its rows" in refused.output


@pytest.mark.asyncio
async def test_a_materialized_view_sorted_by_an_expression_of_hare(round_trip):
    connection = round_trip.connection
    page = build_model(
        "Page",
        "structural_page",
        {"seen": fields.DatetimeField(), "site": fields.CharField(max_length=20)},
        {
            "materialized_views": [
                ClickhouseMaterializedView(
                    "structural_pages_by_day",
                    RawSQLTerm("SELECT seen, site FROM structural_page"),
                    order_by=("site", Trunc("seen", "day")),
                    partition_by=Trunc("seen", "month"),
                )
            ]
        },
    )
    await round_trip.migrate_to(page)
    keys = await get_table_keys(connection, "structural_pages_by_day")
    assert "seen" in keys["create_table_query"]
    assert (await detect_drift(connection, build_live_state(page), [APP_LABEL])).operations == []
    assert round_trip.get_pending_operations(page) == []
