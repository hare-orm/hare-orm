"""The options of a ClickHouse table a migration creates and changes - the compression and the time
to live of its columns, its projections, its settings: changed by ``ALTER TABLE`` where the server
changes them in place, by remaking the table (its rows kept) for its keys."""

import dataclasses
import re

import pytest

from hare import fields
from hare.ddl.raw_sql_term import RawSQLTerm
from hare.dialects.base.features import Features
from hare.dialects.clickhouse.clickhouse_table_options import ClickhouseTableOptions
from hare.dialects.clickhouse.fields import Int8Field
from hare.dialects.clickhouse.schema_objects.clickhouse_projection import ClickhouseProjection
from hare.exceptions import ConfigurationError, UnSupportedError, ValidationError
from hare.migrations.operations import AlterModelOptions, CreateModel, DeleteModel
from hare.migrations.state.state import State
from tests.dialects.clickhouse.models import Team
from tests.utils.operations import apply_operations

TABLE = "clickhouse_visits"
BY_SITE = ClickhouseProjection("by_site", RawSQLTerm("SELECT site, count() GROUP BY site"))
BY_DURATION = ClickhouseProjection("by_duration", RawSQLTerm("SELECT * ORDER BY duration"))
OPTIONS = ClickhouseTableOptions(
    order_by=("id", "site"),
    ttl=RawSQLTerm("toDateTime(seen) + INTERVAL 30 DAY"),
    settings=(("merge_with_ttl_timeout", 3600),),
    column_codecs=(("page", "ZSTD(3)"), ("duration", "Delta, ZSTD")),
    column_ttls=(("page", RawSQLTerm("toDateTime(seen) + INTERVAL 7 DAY")),),
    projections=(BY_SITE,),
)


async def get_table(connection):
    (table,) = await connection.execute_dicts(
        "SELECT toString(uuid) AS uuid, create_table_query, sorting_key FROM system.tables "
        f"WHERE database = currentDatabase() AND name = '{TABLE}'"
    )
    columns = await connection.execute_dicts(
        "SELECT name, compression_codec FROM system.columns "
        f"WHERE database = currentDatabase() AND table = '{TABLE}' AND compression_codec != '' ORDER BY name"
    )
    return (
        table,
        {column["name"]: column["compression_codec"] for column in columns},
        # Read off the table's definition.
        sorted(re.findall(r"PROJECTION (\w+)", table["create_table_query"])),
    )


async def alter(state, editor, options):
    await apply_operations(
        "models",
        state,
        AlterModelOptions(name="Visit", options={"table": TABLE, "table_options": [options]}),
        schema_editor=editor,
    )


@pytest.mark.asyncio
async def test_options_are_created_and_changed_in_place(clickhouse_db):
    connection = Team._meta.connection
    editor = connection.dialect.schema_editor_class(connection, atomic=True, collect_sql=False)
    state = State()
    await apply_operations(
        "models",
        state,
        CreateModel(
            name="Visit",
            fields=[
                ("id", fields.BigIntField(primary_key=True, generated=False)),
                ("site", fields.CharField(max_length=20)),
                ("page", fields.TextField()),
                ("duration", fields.IntField()),
                ("seen", fields.DatetimeField()),
            ],
            options={"table": TABLE, "table_options": [OPTIONS]},
        ),
        schema_editor=editor,
    )
    try:
        table, codecs, projections = await get_table(connection)
        assert codecs == {"duration": "CODEC(Delta(4), ZSTD(1))", "page": "CODEC(ZSTD(3))"}
        assert projections == ["by_site"]
        create_sql = table["create_table_query"]
        assert "TTL toDateTime(seen) + toIntervalDay(7)" in create_sql
        assert "TTL toDateTime(seen) + toIntervalDay(30)" in create_sql
        assert "merge_with_ttl_timeout = 3600" in create_sql
        await connection.execute_script(
            f"INSERT INTO {TABLE} VALUES (1, 'a', 'home', 5, now()), (2, 'a', 'about', 7, now()), "
            "(3, 'b', 'home', 9, now())"
        )

        changed = ClickhouseTableOptions(
            order_by=("id", "site"),
            settings=(("merge_with_ttl_timeout", 7200),),
            column_codecs=(("page", "LZ4"),),
            column_ttls=(
                ("page", RawSQLTerm("toDateTime(seen) + INTERVAL 1 DAY")),
                ("duration", RawSQLTerm("toDateTime(seen) + INTERVAL 2 DAY")),
            ),
            projections=(BY_DURATION,),
        )
        await alter(state, editor, changed)
        altered_table, codecs, projections = await get_table(connection)
        # The same table - nothing was copied.
        assert altered_table["uuid"] == table["uuid"]
        assert codecs == {"page": "CODEC(LZ4)"}
        assert projections == ["by_duration"]
        create_sql = altered_table["create_table_query"]
        assert "toIntervalDay(30)" not in create_sql and "toIntervalDay(2)" in create_sql
        assert "merge_with_ttl_timeout = 7200" in create_sql

        # A table with a projection takes a lightweight DELETE - the server rebuilds the projection.
        await connection.execute_script(f"DELETE FROM {TABLE} WHERE site = 'b'")

        # Another key of the sort remakes the table, its rows kept.
        await alter(state, editor, ClickhouseTableOptions(order_by=("id", "duration"), projections=(BY_DURATION,)))
        remade_table, codecs, projections = await get_table(connection)
        assert remade_table["uuid"] != table["uuid"]
        assert (remade_table["sorting_key"], codecs, projections) == ("id, duration", {}, ["by_duration"])
        rows = await connection.execute_dicts(f"SELECT id, page FROM {TABLE} ORDER BY id")
        assert [(row["id"], row["page"]) for row in rows] == [(1, "home"), (2, "about")]

        # A remade table keeps the codecs and the times to live of its columns.
        kept_column_options = ClickhouseTableOptions(
            order_by=("id", "site"),
            column_codecs=(("page", "LZ4"),),
            column_ttls=(("page", RawSQLTerm("toDateTime(seen) + INTERVAL 1 DAY")),),
            projections=(BY_DURATION,),
        )
        await alter(state, editor, kept_column_options)
        await alter(state, editor, dataclasses.replace(kept_column_options, order_by=("id", "duration")))
        kept_table, codecs, _projections = await get_table(connection)
        assert kept_table["sorting_key"] == "id, duration"
        assert codecs == {"page": "CODEC(LZ4)"}
        assert "TTL toDateTime(seen) + toIntervalDay(1)" in kept_table["create_table_query"]
        await alter(state, editor, ClickhouseTableOptions(order_by=("id", "duration"), projections=(BY_DURATION,)))

        # The settings the projections were taken with go with the last of them.
        await alter(state, editor, ClickhouseTableOptions(order_by=("id", "duration")))
        unprojected_table, _codecs, projections = await get_table(connection)
        assert projections == []
        assert "projection_mode" not in unprojected_table["create_table_query"]
    finally:
        await apply_operations("models", state, DeleteModel(name="Visit"), schema_editor=editor)


@pytest.mark.database_independent
def test_options_are_checked():
    for arguments in (
        {"column_codecs": (("page", "ZSTD(3)); DROP TABLE x"),)},
        {"column_codecs": {"page": "ZSTD"}},
        {"column_ttls": (("page", "seen + 1"),)},
        {"projections": (BY_SITE, BY_SITE)},
        {"projections": ("by_site",)},
    ):
        with pytest.raises(ConfigurationError):
            ClickhouseTableOptions(**arguments)
    for arguments in (
        (" ", RawSQLTerm("SELECT 1")),
        ("by_site", RawSQLTerm("DROP TABLE x")),
        ("p", "SELECT 1"),
    ):
        with pytest.raises(ConfigurationError):
            ClickhouseProjection(*arguments)
    # A key keeps its values, and a column must be one of the model's.
    for options in (
        ClickhouseTableOptions(column_ttls=(("id", RawSQLTerm("now()")),)),
        ClickhouseTableOptions(column_codecs=(("missing", "LZ4"),)),
    ):
        with pytest.raises(ConfigurationError):
            options.raise_if_unsupported(Team, Team._meta.connection.features)
    # A key or a time naming no field was a KeyError of the DDL.
    for option_name, arguments in (
        ("partition_by", {"partition_by": "missing"}),
        ("ttl", {"ttl": "missing"}),
        ("sharding_key", {"engine": "Distributed", "distributed_over": "team_local", "sharding_key": "missing"}),
    ):
        with pytest.raises(ConfigurationError, match=rf"{option_name} names no field \['missing'\]"):
            ClickhouseTableOptions(**arguments).raise_if_unsupported(Team, Features())
    # Rows are sampled by an unsigned integer.
    with pytest.raises(ConfigurationError, match="sample_by 'name' is a column of String"):
        ClickhouseTableOptions(order_by=("id", "name"), sample_by="name").raise_if_unsupported(Team, Features())


@pytest.mark.asyncio
async def test_rows_of_a_table_with_a_projection_are_deleted(clickhouse_db):
    from tests.dialects.clickhouse.models import Trade

    await Trade.objects.bulk_create(
        [Trade(id=number, symbol="ab"[number % 2], traded_at=number) for number in range(6)]
    )
    await Trade.objects.filter(symbol="a").delete()
    assert await Trade.objects.order_by("id").values_list("id", flat=True) == [1, 3, 5]
    (table,) = await Trade._meta.connection.execute_dicts(
        "SELECT create_table_query FROM system.tables WHERE database = currentDatabase() AND name = 'trade'"
    )
    assert re.findall(r"PROJECTION (\w+)", table["create_table_query"]) == ["by_symbol"]


@pytest.mark.asyncio
async def test_a_table_of_an_engine_outside_the_merge_tree_family(clickhouse_db):
    connection = Team._meta.connection
    editor = connection.dialect.schema_editor_class(connection, atomic=True, collect_sql=False)
    state = State()
    await apply_operations(
        "models",
        state,
        CreateModel(
            name="Cache",
            fields=[("id", fields.BigIntField(primary_key=True, generated=False)), ("value", fields.TextField())],
            options={"table": "clickhouse_cache", "table_options": [ClickhouseTableOptions(engine="Memory")]},
        ),
        schema_editor=editor,
    )
    try:
        (table,) = await connection.execute_dicts(
            "SELECT engine_full FROM system.tables WHERE database = currentDatabase() AND name = 'clickhouse_cache'"
        )
        assert table["engine_full"] == "Memory"
    finally:
        await apply_operations("models", state, DeleteModel(name="Cache"), schema_editor=editor)
    for options in (
        ClickhouseTableOptions(engine="Memory", order_by=("id",)),
        ClickhouseTableOptions(engine="Log", settings=(("index_granularity", 1),)),
    ):
        with pytest.raises(ConfigurationError, match="MergeTree family"):
            options.raise_if_unsupported(Team, connection.features)


@pytest.mark.asyncio
async def test_a_collapsing_table_takes_its_sign_column_of_int8(clickhouse_db):
    connection = Team._meta.connection
    editor = connection.dialect.schema_editor_class(connection, atomic=True, collect_sql=False)
    state = State()
    await apply_operations(
        "models",
        state,
        CreateModel(
            name="Balance",
            fields=[
                ("id", fields.BigIntField(primary_key=True, generated=False)),
                ("amount", fields.IntField()),
                ("sign", Int8Field()),
            ],
            options={
                "table": "clickhouse_balance",
                "table_options": [ClickhouseTableOptions(engine="CollapsingMergeTree(sign)")],
            },
        ),
        schema_editor=editor,
    )
    try:
        await connection.execute_script(
            "INSERT INTO clickhouse_balance VALUES (1, 5, 1), (1, 5, -1), (1, 7, 1), (2, 3, 1)"
        )
        rows = await connection.execute_dicts("SELECT id, amount FROM clickhouse_balance FINAL ORDER BY id")
        assert [(row["id"], row["amount"]) for row in rows] == [(1, 7), (2, 3)]
    finally:
        await apply_operations("models", state, DeleteModel(name="Balance"), schema_editor=editor)
    sign = Int8Field()
    sign.model_field_name = "sign"
    with pytest.raises(ValidationError):
        sign.validate(128)


@pytest.mark.asyncio
async def test_a_ttl_of_a_nullable_column_is_refused_with_its_remedy(clickhouse_db):
    connection = Team._meta.connection
    sql = "CREATE TABLE ttl_of_a_nullable (id UInt8, seen Nullable(DateTime)) ENGINE = MergeTree ORDER BY id TTL {}"
    try:
        with pytest.raises(UnSupportedError, match=r"never NULL - the column is nullable: assumeNotNull\(seen\)"):
            await connection.execute_script(sql.format("seen + INTERVAL 1 DAY"))
        await connection.execute_script(sql.format("assumeNotNull(seen) + INTERVAL 1 DAY"))
    finally:
        await connection.execute_script("DROP TABLE IF EXISTS ttl_of_a_nullable")


@pytest.mark.asyncio
async def test_a_relation_named_in_the_sort_sorts_by_its_key_column(clickhouse_db):
    connection = Team._meta.connection
    editor = connection.dialect.schema_editor_class(connection, atomic=True, collect_sql=False)
    state = State()
    await apply_operations(
        "models",
        state,
        CreateModel(
            name="Site",
            fields=[("id", fields.BigIntField(primary_key=True, generated=False))],
            options={"table": "clickhouse_sites"},
        ),
        schema_editor=editor,
    )
    await apply_operations(
        "models",
        state,
        CreateModel(
            name="Visit",
            fields=[
                ("id", fields.BigIntField(primary_key=True, generated=False)),
                ("site", fields.ForeignKeyField("models.Site", related_name="visits")),
                ("seen", fields.DatetimeField()),
            ],
            options={"table": TABLE, "table_options": [ClickhouseTableOptions(order_by=("id", "site", "seen"))]},
        ),
        schema_editor=editor,
    )
    try:
        table, _codecs, _projections = await get_table(connection)
        assert table["sorting_key"] == "id, site_id, seen"
    finally:
        await apply_operations("models", state, DeleteModel(name="Visit"), schema_editor=editor)
        await apply_operations("models", state, DeleteModel(name="Site"), schema_editor=editor)
