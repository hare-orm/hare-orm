"""The options of a ClickHouse table read back from the server - its engine, keys, time to live,
settings, the compression and the time to live of its columns, its projections: rebuilt by inspectdb,
and compared by drift with the declared ones as the server writes them."""

import pytest

from hare.ddl.raw_sql_term import RawSQLTerm
from hare.dialects.clickhouse.clickhouse_introspector import ClickhouseIntrospector
from hare.dialects.clickhouse.clickhouse_table_options import ClickhouseTableOptions
from hare.dialects.clickhouse.introspection.clickhouse_observed_table_options import ClickhouseObservedTableOptions
from hare.dialects.clickhouse.introspection.clickhouse_sql_parts import ClickhouseSqlParts
from hare.dialects.clickhouse.introspection.constants import CLICKHOUSE_ENGINE_CLAUSES
from hare.dialects.clickhouse.schema_objects.clickhouse_projection import ClickhouseProjection
from hare.inspectdb.generation.model_source_generator import ModelSourceGenerator
from hare.inspectdb.introspection.database_catalog import DatabaseCatalog
from hare.migrations.drift import detect_drift
from hare.migrations.operations import AlterModelOptions
from hare.migrations.state.model_state import ModelState
from hare.migrations.state.state import State
from hare.migrations.state.state_apps import StateApps
from tests.dialects.clickhouse import models
from tests.dialects.clickhouse.models import Hit, Team

OBSERVED = ClickhouseTableOptions(
    engine="ReplacingMergeTree(duration_ms)",
    order_by=("id", "site", RawSQLTerm("intHash32(id)")),
    sample_by=RawSQLTerm("intHash32(id)"),
    partition_by=RawSQLTerm("toYYYYMM(seen)"),
    ttl=RawSQLTerm("toDateTime(seen) + toIntervalDay(30)"),
    settings=(("merge_with_ttl_timeout", 3600),),
    column_codecs=(("page", "ZSTD(3)"), ("duration_ms", "Delta(4), ZSTD(1)")),
    column_ttls=(("page", RawSQLTerm("toDateTime(seen) + toIntervalDay(7)")),),
    projections=(ClickhouseProjection("by_site", RawSQLTerm("SELECT site, count() GROUP BY site")),),
)


async def get_table_options_changes(connection, model):
    state = State(models={}, apps=StateApps())
    state.models[("models", model.__name__)] = ModelState.make_from_model("models", model)
    drift = await detect_drift(connection, state, ["models"])
    return [operation for operation in drift.operations if isinstance(operation, AlterModelOptions)]


def test_sql_is_split_outside_strings_and_parentheses():
    assert ClickhouseSqlParts.split("a, f(b, c), 'd, e', `g, h`") == ["a", "f(b, c)", "'d, e'", "`g, h`"]
    assert ClickhouseSqlParts.split("  ") == []
    assert ClickhouseSqlParts.find_words("f(TTL) + 'TTL' + xTTL TTL y", "TTL") == 22
    assert ClickhouseSqlParts.find_words("no such word", "TTL") == -1
    assert ClickhouseSqlParts.get_parenthesised("t (a (b) ')' c) ENGINE = x(y)") == ("a (b) ')' c", " ENGINE = x(y)")
    assert ClickhouseSqlParts.get_leading_identifier("`a \\` b` String") == ("a ` b", " String")
    assert ClickhouseSqlParts.get_identifier(" `a b` ") == "a b"
    assert ClickhouseSqlParts.get_identifier("intHash32(id)") is None


def test_the_engine_and_the_column_list_are_read():
    assert ClickhouseSqlParts.get_clauses(
        "ReplicatedMergeTree('/t/{shard} ORDER BY x', '{replica}') PARTITION BY toYYYYMM(d) ORDER BY (a, b) "
        "TTL d + toIntervalDay(3) WHERE v = 'SETTINGS', d + toIntervalDay(9) SETTINGS a = 1, b = 'it\\'s, x'",
        CLICKHOUSE_ENGINE_CLAUSES,
    ) == {
        "": "ReplicatedMergeTree('/t/{shard} ORDER BY x', '{replica}')",
        "PARTITION BY": "toYYYYMM(d)",
        "ORDER BY": "(a, b)",
        "TTL": "d + toIntervalDay(3) WHERE v = 'SETTINGS', d + toIntervalDay(9)",
        "SETTINGS": "a = 1, b = 'it\\'s, x'",
    }
    assert ClickhouseSqlParts.get_clauses("Memory", CLICKHOUSE_ENGINE_CLAUSES) == {"": "Memory"}
    assert ClickhouseSqlParts.get_before_parentheses(" default.t (`a` String) ") == "default.t"
    assert ClickhouseObservedTableOptions.get_settings("a = 1, b = 'it\\'s, x', c = 0.5") == {
        "a": 1,
        "b": "it's, x",
        "c": "0.5",
    }
    assert ClickhouseObservedTableOptions.get_column_list_options(
        "CREATE TABLE d.`t (x)` (`id` Int64, `a b` String COMMENT 'no TTL, here' CODEC(ZSTD(1)) "
        "TTL toDate(seen) + toIntervalDay(1), `seen` DateTime, `v` UInt8 DEFAULT 1 TTL seen + toIntervalDay(2) "
        "SETTINGS (max_compress_block_size = 8), INDEX ix v TYPE minmax GRANULARITY 1, "
        "PROJECTION `p p` (SELECT * ORDER BY v)) ENGINE = MergeTree ORDER BY id",
        {"id", "a b", "seen", "v"},
    ) == (
        [("a b", RawSQLTerm("toDate(seen) + toIntervalDay(1)")), ("v", RawSQLTerm("seen + toIntervalDay(2)"))],
        [ClickhouseProjection("p p", RawSQLTerm("SELECT * ORDER BY v"))],
    )


@pytest.mark.asyncio
async def test_options_are_read_back_and_written_by_inspectdb(clickhouse_db):
    connection = Hit._meta.connection
    table = await DatabaseCatalog.inspect_table(connection, "hit")
    assert table.table_options == OBSERVED
    # A table of no options of its own, and one sorted by more than its key.
    assert (await DatabaseCatalog.inspect_table(connection, "team")).table_options is None
    page_views = await DatabaseCatalog.inspect_table(connection, "pageview")
    assert page_views.table_options == ClickhouseTableOptions(
        order_by=("id", "site", "viewed_at"),
        partition_by=RawSQLTerm("toYYYYMM(viewed_at)"),
        settings=(("index_granularity", 1024),),
    )
    source = ModelSourceGenerator.generate_model_source(table, connection.dialect.name)
    compile(source, "<generated>", "exec")
    for expected in (
        "engine='ReplacingMergeTree(duration_ms)'",
        "order_by=('id', 'site', RawSQLTerm('intHash32(id)'))",
        "column_codecs=(('page', 'ZSTD(3)'), ('duration_ms', 'Delta(4), ZSTD(1)'))",
        "ClickhouseProjection(name='by_site', query=RawSQLTerm('SELECT site, count() GROUP BY site'))",
    ):
        assert expected in source, (expected, source)


@pytest.mark.asyncio
async def test_drift_finds_no_change_of_the_declared_options(clickhouse_db):
    connection = Team._meta.connection
    state = State(models={}, apps=StateApps())
    for name in ("Hit", "Team", "PageView", "Reading", "Trade"):
        state.models[("models", name)] = ModelState.make_from_model("models", getattr(models, name))
    drift = await detect_drift(connection, state, ["models"])
    assert [operation for operation in drift.operations if isinstance(operation, AlterModelOptions)] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("change_sql", "restore_sql", "option_name", "value"),
    [
        (
            "MODIFY SETTING merge_with_ttl_timeout = 7200",
            "MODIFY SETTING merge_with_ttl_timeout = 3600",
            "settings",
            (("merge_with_ttl_timeout", 7200),),
        ),
        (
            "MODIFY TTL toDateTime(seen) + INTERVAL 31 DAY",
            "MODIFY TTL toDateTime(seen) + INTERVAL 30 DAY",
            "ttl",
            RawSQLTerm("toDateTime(seen) + toIntervalDay(31)"),
        ),
        (
            "MODIFY COLUMN duration_ms Int32 CODEC(Delta, LZ4)",
            "MODIFY COLUMN duration_ms Int32 CODEC(Delta, ZSTD)",
            "column_codecs",
            (("page", "ZSTD(3)"), ("duration", "Delta(4), LZ4")),
        ),
        (
            "MODIFY COLUMN page String TTL toDateTime(seen) + INTERVAL 8 DAY",
            "MODIFY COLUMN page String TTL toDateTime(seen) + INTERVAL 7 DAY",
            "column_ttls",
            (("page", RawSQLTerm("toDateTime(seen) + toIntervalDay(8)")),),
        ),
        (
            "ADD PROJECTION by_page (SELECT page, count() GROUP BY page)",
            "DROP PROJECTION by_page",
            "projections",
            (
                ClickhouseProjection("by_site", RawSQLTerm("SELECT site, count() GROUP BY site")),
                ClickhouseProjection("by_page", RawSQLTerm("SELECT page, count() GROUP BY page")),
            ),
        ),
    ],
)
async def test_drift_finds_a_changed_option(clickhouse_db, change_sql, restore_sql, option_name, value):
    connection = Hit._meta.connection
    (declared,) = Hit._meta.table_options
    await connection.execute_script(f"ALTER TABLE hit {change_sql}")
    try:
        assert len(await get_table_options_changes(connection, Hit)) == 1
        table = await DatabaseCatalog.inspect_table(connection, "hit")
        observed = await ClickhouseIntrospector.fetch_declared_table_options(
            connection, table.table_options, declared, table, {"duration_ms": "duration"}
        )
        changed = {
            option: getattr(observed, option)
            for option in ClickhouseTableOptions.__dataclass_fields__
            if getattr(observed, option) != getattr(declared, option)
        }
        assert changed == {option_name: value}
    finally:
        await connection.execute_script(f"ALTER TABLE hit {restore_sql}")
    assert await get_table_options_changes(connection, Hit) == []
