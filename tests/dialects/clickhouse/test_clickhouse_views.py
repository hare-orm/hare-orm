"""Views and materialized views on ClickHouse - a view of the ``View`` engine, a materialized view
following the inserts of the table it reads into a storage of its own or into another table, and one
refreshed on a schedule: created with the model, added, changed, renamed, refreshed and removed by
migrations, kept over a remade table, and compared by drift."""

import dataclasses

import pytest

from hare import fields
from hare.ddl.raw_sql_term import RawSQLTerm
from hare.ddl.schema_objects.materialized_view import MaterializedView
from hare.ddl.schema_objects.view import View
from hare.dialects.clickhouse.clickhouse_table_options import ClickhouseTableOptions
from hare.dialects.clickhouse.schema_objects import ClickhouseMaterializedView
from hare.exceptions import ConfigurationError, UnSupportedError
from hare.migrations.drift import detect_drift
from hare.migrations.operations import (
    AddMaterializedView,
    AddView,
    AlterMaterializedView,
    AlterModelOptions,
    AlterView,
    CreateModel,
    DeleteModel,
    RefreshMaterializedView,
    RemoveMaterializedView,
    RemoveView,
    RenameMaterializedView,
    RenameView,
)
from hare.migrations.state.model_state import ModelState
from hare.migrations.state.state import State
from hare.migrations.state.state_apps import StateApps
from tests.dialects.clickhouse.models import Sale, ShopTotal

SALES = [(1, "north", 50), (2, "north", 150), (3, "south", 500)]


async def get_rows(connection, sql):
    return [tuple(row.values()) for row in await connection.execute_dicts(sql)]


async def get_engines(connection, prefix):
    rows = await connection.execute_dicts(
        "SELECT name, engine FROM system.tables WHERE database = currentDatabase() "
        f"AND name LIKE '{prefix}%' ORDER BY name"
    )
    return {row["name"]: row["engine"] for row in rows}


async def create_sales(connection):
    # A materialized view's own storage isn't one of the tables emptied between the tests.
    await connection.execute_script("TRUNCATE TABLE sales_by_shop")
    await Sale.objects.bulk_create([Sale(id=sale_id, shop=shop, amount=amount) for sale_id, shop, amount in SALES])


async def run(operation, state, editor):
    await operation.run("models", state, dry_run=False, state_editor=editor)


@pytest.mark.asyncio
async def test_views_are_created_with_the_model_and_follow_its_rows(clickhouse_db):
    connection = Sale._meta.connection
    assert await get_engines(connection, "sale") == {
        "sale": "MergeTree",
        "sale_totals": "MaterializedView",
        "sales_by_shop": "MaterializedView",
    }
    assert (await get_engines(connection, "big_sales")) == {"big_sales": "View"}
    await create_sales(connection)
    assert await get_rows(connection, "SELECT id FROM big_sales ORDER BY id") == [(2,), (3,)]
    totals_sql = "SELECT shop, sum(total) FROM {} GROUP BY shop ORDER BY shop"
    assert await get_rows(connection, totals_sql.format("sales_by_shop")) == [("north", 200), ("south", 500)]
    assert await get_rows(connection, totals_sql.format("shop_total")) == [("north", 200), ("south", 500)]
    assert await ShopTotal.objects.count() >= 2

    # A refresh fills the view by its whole query again - the rows it held are replaced.
    await connection.execute_script("ALTER TABLE sale DELETE WHERE id = 3 SETTINGS mutations_sync = 2")
    await Sale.objects.refresh_materialized_view("sales_by_shop")
    assert await get_rows(connection, "SELECT shop, total FROM sales_by_shop ORDER BY shop") == [("north", 200)]
    await Sale.objects.refresh_materialized_view("sale_totals")
    assert await get_rows(connection, totals_sql.format("shop_total")) == [("north", 200)]
    with pytest.raises(UnSupportedError):
        await Sale.objects.refresh_materialized_view("sales_by_shop", concurrently=True)


@pytest.mark.asyncio
async def test_drift_finds_no_change_of_the_declared_views_and_a_changed_one(clickhouse_db):
    connection = Sale._meta.connection
    state = State(models={}, apps=StateApps())
    for model in (Sale, ShopTotal):
        state.models[("models", model.__name__)] = ModelState.make_from_model("models", model)

    async def get_view_operations():
        drift = await detect_drift(connection, state, ["models"])
        return [
            (type(operation).__name__, operation.view.name)
            for operation in drift.operations
            if "View" in type(operation).__name__
        ]

    assert await get_view_operations() == []
    await connection.execute_script(
        "CREATE OR REPLACE VIEW big_sales AS SELECT id, shop, amount FROM sale WHERE amount > 200;"
        "ALTER TABLE sale_totals MODIFY QUERY SELECT shop, toInt64(amount * 2) AS total FROM sale;"
        "DROP VIEW sales_by_shop;"
    )
    try:
        assert sorted(await get_view_operations()) == [
            ("AddMaterializedView", "sales_by_shop"),
            ("AlterMaterializedView", "sale_totals"),
            ("AlterView", "big_sales"),
        ]
        await connection.execute_script(
            "CREATE MATERIALIZED VIEW sales_by_shop ENGINE = SummingMergeTree ORDER BY (shop) AS "
            "SELECT shop, toInt64(sum(amount)) AS total FROM sale GROUP BY shop"
        )
        # The view of another engine than the declared one.
        assert ("AlterMaterializedView", "sales_by_shop") in await get_view_operations()
    finally:
        editor = connection.dialect.schema_editor_class(connection, atomic=True, collect_sql=False)
        await editor.materialized_views.drop_model_materialized_views(Sale)
        await editor.run_sqls(editor.table_creation.get_schema_objects_after_table_sqls(Sale, safe=True))
    assert await get_view_operations() == []


@pytest.mark.asyncio
async def test_migrations_add_change_rename_and_remove_views(clickhouse_db):
    connection = Sale._meta.connection
    editor = connection.dialect.schema_editor_class(connection, atomic=True, collect_sql=False)
    state = State(models={}, apps=StateApps())
    await run(
        CreateModel(
            name="Parcel",
            fields=[
                ("id", fields.BigIntField(primary_key=True, generated=False)),
                ("city", fields.CharField(max_length=20)),
                ("weight", fields.IntField()),
            ],
            options={"table": "views_parcel"},
        ),
        state,
        editor,
    )
    await run(
        CreateModel(
            name="CityWeight",
            fields=[("city", fields.CharField(max_length=20, primary_key=True)), ("weight", fields.BigIntField())],
            options={
                "table": "views_city_weight",
                "table_options": [ClickhouseTableOptions(engine="SummingMergeTree")],
            },
        ),
        state,
        editor,
    )
    heavy = View("views_heavy", RawSQLTerm("SELECT id FROM views_parcel WHERE weight > 10"))
    by_city_sql = "SELECT city, toInt64(sum(weight)) AS weight FROM views_parcel GROUP BY city"
    by_city = ClickhouseMaterializedView(
        "views_by_city", RawSQLTerm(by_city_sql), engine="SummingMergeTree", order_by=("city",)
    )
    to_city = ClickhouseMaterializedView(
        "views_to_city", RawSQLTerm("SELECT city, toInt64(weight) AS weight FROM views_parcel"), to="views_city_weight"
    )
    try:
        await connection.execute_script("INSERT INTO views_parcel VALUES (1, 'a', 5), (2, 'a', 20), (3, 'b', 30)")
        await run(AddView("Parcel", heavy), state, editor)
        await run(AddMaterializedView("Parcel", by_city), state, editor)
        await run(AddMaterializedView("Parcel", to_city), state, editor)
        # Both are filled with the rows the table held.
        totals_sql = "SELECT city, sum(weight) FROM {} GROUP BY city ORDER BY city"
        assert await get_rows(connection, "SELECT id FROM views_heavy ORDER BY id") == [(2,), (3,)]
        assert await get_rows(connection, totals_sql.format("views_by_city")) == [("a", 25), ("b", 30)]
        assert await get_rows(connection, totals_sql.format("views_city_weight")) == [("a", 25), ("b", 30)]

        await run(AlterView("Parcel", View("views_heavy", RawSQLTerm("SELECT id FROM views_parcel"))), state, editor)
        assert len(await get_rows(connection, "SELECT id FROM views_heavy")) == 3
        # The query of a view writing to a table changes in place - the table keeps its rows; a view of
        # its own storage is created anew and filled by the new query.
        doubled = ClickhouseMaterializedView(
            "views_to_city",
            RawSQLTerm("SELECT city, toInt64(weight * 2) AS weight FROM views_parcel"),
            to="views_city_weight",
        )
        await run(AlterMaterializedView("Parcel", doubled), state, editor)
        counted = ClickhouseMaterializedView(
            "views_by_city",
            RawSQLTerm("SELECT city, toInt64(count()) AS weight FROM views_parcel GROUP BY city"),
            engine="SummingMergeTree",
            order_by=("city",),
        )
        await run(AlterMaterializedView("Parcel", counted), state, editor)
        await connection.execute_script("INSERT INTO views_parcel VALUES (4, 'b', 1)")
        assert await get_rows(connection, totals_sql.format("views_city_weight")) == [("a", 25), ("b", 32)]
        assert await get_rows(connection, totals_sql.format("views_by_city")) == [("a", 2), ("b", 2)]

        await run(RenameView("Parcel", "views_heavy", "views_all"), state, editor)
        await run(RenameMaterializedView("Parcel", "views_by_city", "views_per_city"), state, editor)
        assert await get_engines(connection, "views_") == {
            "views_all": "View",
            "views_city_weight": "SummingMergeTree",
            "views_parcel": "MergeTree",
            "views_per_city": "MaterializedView",
            "views_to_city": "MaterializedView",
        }
        await run(RefreshMaterializedView("Parcel", "views_to_city"), state, editor)
        assert await get_rows(connection, totals_sql.format("views_city_weight")) == [("a", 50), ("b", 62)]

        # A remade table has its views over it again: they follow its new rows, the copied ones not
        # taken twice.
        await run(
            AlterModelOptions(
                name="Parcel",
                options={"table": "views_parcel", "table_options": [ClickhouseTableOptions(order_by=("id", "city"))]},
            ),
            state,
            editor,
        )
        assert await get_rows(connection, totals_sql.format("views_per_city")) == [("a", 2), ("b", 2)]
        assert await get_rows(connection, totals_sql.format("views_city_weight")) == [("a", 50), ("b", 62)]
        await connection.execute_script("INSERT INTO views_parcel VALUES (5, 'c', 7)")
        assert await get_rows(connection, totals_sql.format("views_per_city")) == [("a", 2), ("b", 2), ("c", 1)]
        assert len(await get_rows(connection, "SELECT id FROM views_all")) == 5

        await run(RemoveView("Parcel", "views_all"), state, editor)
        await run(RemoveMaterializedView("Parcel", "views_per_city"), state, editor)
        assert "views_all" not in await get_engines(connection, "views_")
    finally:
        # The views a model declares go with its table.
        await run(DeleteModel(name="Parcel"), state, editor)
        await run(DeleteModel(name="CityWeight"), state, editor)
    assert await get_engines(connection, "views_") == {}


@pytest.mark.asyncio
async def test_a_view_is_refreshed_on_a_schedule(clickhouse_db):
    connection = Sale._meta.connection
    if not connection.features.supports_refreshable_materialized_views:
        with pytest.raises(UnSupportedError):
            editor = connection.dialect.schema_editor_class(connection, atomic=True, collect_sql=True)
            editor.materialized_views.get_materialized_view_create_sqls(
                Sale, ClickhouseMaterializedView("hourly", RawSQLTerm("SELECT 1"), refresh="EVERY 1 HOUR")
            )
        return
    editor = connection.dialect.schema_editor_class(connection, atomic=True, collect_sql=False)
    state = State(models={}, apps=StateApps())
    state.models[("models", "Sale")] = ModelState.make_from_model("models", Sale)
    await create_sales(connection)
    snapshot = ClickhouseMaterializedView(
        "sales_snapshot",
        RawSQLTerm("SELECT shop, toInt64(count()) AS sales FROM sale GROUP BY shop"),
        refresh="every 1 hours",
        order_by=("shop",),
    )
    await run(AddMaterializedView("Sale", snapshot), state, editor)
    try:
        rows_sql = "SELECT shop, sales FROM sales_snapshot ORDER BY shop"
        # Created with its rows - the first refresh is waited for.
        assert await get_rows(connection, rows_sql) == [("north", 2), ("south", 1)]
        await Sale.objects.create(id=4, shop="south", amount=1)
        assert await get_rows(connection, rows_sql) == [("north", 2), ("south", 1)]
        await run(RefreshMaterializedView("Sale", "sales_snapshot", concurrently=True), state, editor)
        assert await get_rows(connection, rows_sql) == [("north", 2), ("south", 2)]

        drift = await detect_drift(connection, state, ["models"])
        assert [operation for operation in drift.operations if "View" in type(operation).__name__] == []
        every_two_hours = dataclasses.replace(snapshot, refresh="EVERY 2 HOUR")
        await run(AlterMaterializedView("Sale", every_two_hours), state, editor)
        (definition,) = await get_rows(
            connection,
            "SELECT create_table_query FROM system.tables "
            "WHERE database = currentDatabase() AND name = 'sales_snapshot'",
        )
        assert "REFRESH EVERY 2 HOUR" in definition[0]
        # Changed in place - the rows are the ones refreshed before.
        assert await get_rows(connection, rows_sql) == [("north", 2), ("south", 2)]
    finally:
        await run(RemoveMaterializedView("Sale", "sales_snapshot"), state, editor)


def test_a_materialized_view_is_checked():
    query = RawSQLTerm("SELECT 1 AS one")
    for arguments in (
        {"to": " "},
        {"to": "target", "engine": "SummingMergeTree"},
        {"to": "target", "order_by": ("one",)},
        {"engine": ""},
        {"order_by": "one"},
        {"partition_by": "one"},
        {"refresh": "sometimes"},
        {"refresh": "EVERY 1 HOUR; DROP TABLE x"},
        {"append": True},
        {"depends_on": ("other",)},
        {"refresh": "EVERY 1 HOUR", "depends_on": "other"},
    ):
        with pytest.raises(ConfigurationError):
            ClickhouseMaterializedView("checked", query, **arguments)
    view = ClickhouseMaterializedView(
        "checked", query, refresh="EVERY 1 DAY OFFSET 2 HOUR RANDOMIZE FOR 30 MINUTE", append=True, to="target"
    )
    path, arguments, options = view.deconstruct()
    assert path.endswith("ClickhouseMaterializedView") and arguments == []
    assert options == {
        "name": "checked",
        "query": query,
        "to": "target",
        "refresh": "EVERY 1 DAY OFFSET 2 HOUR RANDOMIZE FOR 30 MINUTE",
        "append": True,
    }
    assert ClickhouseMaterializedView.from_materialized_view(view) is view
    plain = MaterializedView("plain", query, unique_columns=("one",))
    assert ClickhouseMaterializedView.from_materialized_view(plain).get_sort_keys() == ("one",)
