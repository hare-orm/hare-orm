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
from hare.migrations.state.state_apps import StateApps
from tests.dialects.clickhouse.models import Team

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
        # Read off the table's definition - ClickHouse 24.3 has no system.projections.
        sorted(re.findall(r"PROJECTION (\w+)", table["create_table_query"])),
    )


async def alter(state, editor, options):
    await AlterModelOptions(name="Visit", options={"table": TABLE, "table_options": [options]}).run(
        "models", state, dry_run=False, state_editor=editor
    )


@pytest.mark.asyncio
async def test_options_are_created_and_changed_in_place(clickhouse_db):
    connection = Team._meta.connection
    editor = connection.dialect.schema_editor_class(connection, atomic=True, collect_sql=False)
    state = State(models={}, apps=StateApps())
    await CreateModel(
        name="Visit",
        fields=[
            ("id", fields.BigIntField(primary_key=True, generated=False)),
            ("site", fields.CharField(max_length=20)),
            ("page", fields.TextField()),
            ("duration", fields.IntField()),
            ("seen", fields.DatetimeField()),
        ],
        options={"table": TABLE, "table_options": [OPTIONS]},
    ).run("models", state, dry_run=False, state_editor=editor)
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

        # A table with a projection takes a lightweight DELETE where the server rebuilds projections.
        if connection.features.rebuilds_projections:
            await connection.execute_script(f"DELETE FROM {TABLE} WHERE site = 'b'")
        else:
            await connection.execute_script(f"ALTER TABLE {TABLE} DELETE WHERE site = 'b'")

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
        await DeleteModel(name="Visit").run("models", state, dry_run=False, state_editor=editor)


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
    # Rows are sampled by an unsigned integer.
    with pytest.raises(ConfigurationError, match="sample_by 'name' is a column of String"):
        ClickhouseTableOptions(order_by=("id", "name"), sample_by="name").raise_if_unsupported(Team, Features())


@pytest.mark.asyncio
async def test_rows_of_a_table_with_a_projection_are_deleted(clickhouse_db):
    from tests.dialects.clickhouse.models import Trade

    await Trade.objects.bulk_create(
        [Trade(id=number, symbol="ab"[number % 2], traded_at=number) for number in range(6)]
    )
    assert await Trade.objects.filter(symbol="a").delete() == 3
    assert await Trade.objects.order_by("id").values_list("id", flat=True) == [1, 3, 5]
    (table,) = await Trade._meta.connection.execute_dicts(
        "SELECT create_table_query FROM system.tables WHERE database = currentDatabase() AND name = 'trade'"
    )
    assert re.findall(r"PROJECTION (\w+)", table["create_table_query"]) == ["by_symbol"]


@pytest.mark.asyncio
async def test_a_table_of_an_engine_outside_the_merge_tree_family(clickhouse_db):
    connection = Team._meta.connection
    editor = connection.dialect.schema_editor_class(connection, atomic=True, collect_sql=False)
    state = State(models={}, apps=StateApps())
    await CreateModel(
        name="Cache",
        fields=[("id", fields.BigIntField(primary_key=True, generated=False)), ("value", fields.TextField())],
        options={"table": "clickhouse_cache", "table_options": [ClickhouseTableOptions(engine="Memory")]},
    ).run("models", state, dry_run=False, state_editor=editor)
    try:
        (table,) = await connection.execute_dicts(
            "SELECT engine_full FROM system.tables WHERE database = currentDatabase() AND name = 'clickhouse_cache'"
        )
        assert table["engine_full"] == "Memory"
    finally:
        await DeleteModel(name="Cache").run("models", state, dry_run=False, state_editor=editor)
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
    state = State(models={}, apps=StateApps())
    await CreateModel(
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
    ).run("models", state, dry_run=False, state_editor=editor)
    try:
        await connection.execute_script(
            "INSERT INTO clickhouse_balance VALUES (1, 5, 1), (1, 5, -1), (1, 7, 1), (2, 3, 1)"
        )
        rows = await connection.execute_dicts("SELECT id, amount FROM clickhouse_balance FINAL ORDER BY id")
        assert [(row["id"], row["amount"]) for row in rows] == [(1, 7), (2, 3)]
    finally:
        await DeleteModel(name="Balance").run("models", state, dry_run=False, state_editor=editor)
    sign = Int8Field()
    sign.model_field_name = "sign"
    with pytest.raises(ValidationError):
        sign.validate(128)


def test_a_projection_of_an_engine_merging_rows_away_needs_a_server_rebuilding_it():
    """ClickHouse 24.3 merges a ReplacingMergeTree's rows away without rebuilding its projections - an
    aggregate projection kept counting the replaced rows."""
    options = ClickhouseTableOptions(engine="ReplacingMergeTree", projections=(BY_SITE,))
    with pytest.raises(UnSupportedError, match="a projection of a ReplacingMergeTree table needs ClickHouse 24.8"):
        options.raise_if_unsupported(Team, Features(rebuilds_projections=False))
    options.raise_if_unsupported(Team, Features(rebuilds_projections=True))
    ClickhouseTableOptions(projections=(BY_SITE,)).raise_if_unsupported(Team, Features(rebuilds_projections=False))


@pytest.mark.asyncio
async def test_a_ttl_of_a_type_the_server_keeps_none_by_is_refused_with_its_remedy(clickhouse_db):
    """ClickHouse 24.3 keeps a TTL of a Date or a DateTime only - a DateField's Date32 and a
    DatetimeField's DateTime64 came back as the server's raw error."""
    connection = Team._meta.connection
    (version,) = (await connection.execute_dicts("SELECT version() AS version"))[0].values()
    sql = (
        "CREATE TABLE ttl_of_a_date32 (id UInt8, day Date32) ENGINE = MergeTree ORDER BY id TTL day + INTERVAL 1 YEAR"
    )
    refused = False
    try:
        await connection.execute_script(sql)
    except UnSupportedError as error:
        refused = True
        assert "convert the moment, toDateTime(seen)" in str(error)
        await connection.execute_script(sql.replace("TTL day", "TTL toDateTime(day)"))
    finally:
        await connection.execute_script("DROP TABLE IF EXISTS ttl_of_a_date32")
    # 24.3 keeps no TTL of a Date32; the newer servers the tests run on do.
    assert refused == (int(version.split(".")[0]) < 25)


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
    state = State(models={}, apps=StateApps())
    await CreateModel(
        name="Site",
        fields=[("id", fields.BigIntField(primary_key=True, generated=False))],
        options={"table": "clickhouse_sites"},
    ).run("models", state, dry_run=False, state_editor=editor)
    await CreateModel(
        name="Visit",
        fields=[
            ("id", fields.BigIntField(primary_key=True, generated=False)),
            ("site", fields.ForeignKeyField("models.Site", related_name="visits")),
            ("seen", fields.DatetimeField()),
        ],
        options={"table": TABLE, "table_options": [ClickhouseTableOptions(order_by=("id", "site", "seen"))]},
    ).run("models", state, dry_run=False, state_editor=editor)
    try:
        table, _codecs, _projections = await get_table(connection)
        assert table["sorting_key"] == "id, site_id, seen"
    finally:
        await DeleteModel(name="Visit").run("models", state, dry_run=False, state_editor=editor)
        await DeleteModel(name="Site").run("models", state, dry_run=False, state_editor=editor)
