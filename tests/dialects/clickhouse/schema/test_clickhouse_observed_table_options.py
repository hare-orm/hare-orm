"""The options of a ClickHouse table read back from the server - its engine, keys, time to live,
settings, the compression and the time to live of its columns, its projections: rebuilt by inspectdb,
and compared by drift with the declared ones as the server writes them."""

import pytest

from hare.ddl.raw_sql_term import RawSQLTerm
from hare.dialects.clickhouse.clickhouse_table_options import ClickhouseTableOptions
from hare.dialects.clickhouse.introspection.clickhouse_observed_table_options import ClickhouseObservedTableOptions
from hare.dialects.clickhouse.introspection.clickhouse_sql_parts import ClickhouseSqlParts
from hare.dialects.clickhouse.introspection.constants import CLICKHOUSE_ENGINE_CLAUSES
from hare.dialects.clickhouse.schema_objects.clickhouse_projection import ClickhouseProjection
from hare.inspectdb.introspection.database_catalog import DatabaseCatalog
from hare.migrations.drift import detect_drift
from hare.migrations.operations import AlterModelOptions
from hare.migrations.state.model_state import ModelState
from hare.migrations.state.state import State
from tests.dialects.clickhouse import models
from tests.dialects.clickhouse.models import Hit, PageView, Team
from tests.utils.model_source_generator import ModelSourceGenerator

OBSERVED = ClickhouseTableOptions(
    engine="ReplacingMergeTree(duration_ms)",
    order_by=("id", "site", RawSQLTerm("intHash32(id)")),
    sample_by=RawSQLTerm("intHash32(id)"),
    partition_by=RawSQLTerm("toYYYYMM(seen)"),
    ttl=RawSQLTerm("toDateTime(seen) + toIntervalDay(30)"),
    settings=(("merge_with_ttl_timeout", 3600),),
    column_codecs=(("page", "ZSTD(3)"), ("duration_ms", "Delta(4), ZSTD(1)")),
    column_ttls=(("page", RawSQLTerm("toDateTime(seen) + toIntervalDay(7)")),),
)


async def get_table_options_changes(connection, model):
    state = State()
    state.models[("models", model.__name__)] = ModelState.make_from_model("models", model)
    drift = await detect_drift(connection, state, ["models"])
    return [operation for operation in drift.operations if isinstance(operation, AlterModelOptions)]


@pytest.mark.database_independent
def test_sql_is_split_outside_strings_and_parentheses():
    assert ClickhouseSqlParts.split("a, f(b, c), 'd, e', `g, h`") == ["a", "f(b, c)", "'d, e'", "`g, h`"]
    assert ClickhouseSqlParts.split("  ") == []
    assert ClickhouseSqlParts.find_words("f(TTL) + 'TTL' + xTTL TTL y", "TTL") == 22
    assert ClickhouseSqlParts.find_words("no such word", "TTL") == -1
    assert ClickhouseSqlParts.get_parenthesised("t (a (b) ')' c) ENGINE = x(y)") == ("a (b) ')' c", " ENGINE = x(y)")
    assert ClickhouseSqlParts.get_leading_identifier("`a \\` b` String") == ("a ` b", " String")
    assert ClickhouseSqlParts.get_identifier(" `a b` ") == "a b"
    assert ClickhouseSqlParts.get_identifier("intHash32(id)") is None


@pytest.mark.database_independent
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
        projections=(ClickhouseProjection("by_site", RawSQLTerm("SELECT site, count() GROUP BY site")),),
    )
    source = ModelSourceGenerator.generate_model_source(table, connection.dialect.name)
    page_view_source = ModelSourceGenerator.generate_model_source(page_views, connection.dialect.name)
    assert (
        "ClickhouseProjection(name='by_site', query=RawSQLTerm('SELECT site, count() GROUP BY site'))"
        in page_view_source
    )
    compile(source, "<generated>", "exec")
    for expected in (
        "engine='ReplacingMergeTree(duration_ms)'",
        "order_by=('id', 'site', RawSQLTerm('intHash32(id)'))",
        "column_codecs=(('page', 'ZSTD(3)'), ('duration_ms', 'Delta(4), ZSTD(1)'))",
    ):
        assert expected in source, (expected, source)


@pytest.mark.asyncio
async def test_drift_finds_no_change_of_the_declared_options(clickhouse_db):
    connection = Team._meta.connection
    state = State()
    for name in ("Hit", "Team", "PageView", "Reading", "Trade"):
        state.models[("models", name)] = ModelState.make_from_model("models", getattr(models, name))
    drift = await detect_drift(connection, state, ["models"])
    assert [operation for operation in drift.operations if isinstance(operation, AlterModelOptions)] == []
    # A sampled table's PRIMARY KEY holds the keys of its sort up to the sampling one, not only the
    # model's primary key - those keys aren't a mismatch.
    assert drift.mismatched_columns == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("change_sql", "restore_sql"),
    [
        ("MODIFY SETTING merge_with_ttl_timeout = 7200", "MODIFY SETTING merge_with_ttl_timeout = 3600"),
        ("MODIFY TTL toDateTime(seen) + INTERVAL 31 DAY", "MODIFY TTL toDateTime(seen) + INTERVAL 30 DAY"),
        (
            "MODIFY COLUMN page String TTL toDateTime(seen) + INTERVAL 8 DAY",
            "MODIFY COLUMN page String TTL toDateTime(seen) + INTERVAL 7 DAY",
        ),
    ],
)
async def test_drift_finds_a_changed_option(clickhouse_db, change_sql, restore_sql):
    connection = Hit._meta.connection
    await connection.execute_script(f"ALTER TABLE hit {change_sql}")
    try:
        assert len(await get_table_options_changes(connection, Hit)) == 1
    finally:
        await connection.execute_script(f"ALTER TABLE hit {restore_sql}")
    assert await get_table_options_changes(connection, Hit) == []


@pytest.mark.asyncio
async def test_drift_finds_an_added_projection(clickhouse_db):
    connection = PageView._meta.connection
    await connection.execute_script(
        "ALTER TABLE pageview ADD PROJECTION by_duration (SELECT duration_ms, count() GROUP BY duration_ms)"
    )
    try:
        assert len(await get_table_options_changes(connection, PageView)) == 1
    finally:
        await connection.execute_script("ALTER TABLE pageview DROP PROJECTION by_duration")
    assert await get_table_options_changes(connection, PageView) == []


@pytest.mark.database_independent
def test_the_version_column_the_server_appends_to_the_sort_is_left_out():
    """A VersionedCollapsingMergeTree table's sorting key ends with its version column - the server
    appends it to every one, so drift saw an ORDER BY no model declared."""
    get_keys = ClickhouseObservedTableOptions.get_declared_sorting_key_sqls
    engine = "VersionedCollapsingMergeTree(sign, version)"
    assert get_keys("id, version", engine, "id") == ["id"]
    # ClickHouse 25.8 appends the version to a sort ending with it too, 26.7+ doesn't.
    assert get_keys("id, version, version", engine, "(id, version)") == ["id", "version"]
    assert get_keys("id, version", engine, "(id, version)") == ["id", "version"]
    # 26.7+ keeps the parentheses of the sort.
    assert get_keys("(id), version", engine, "(id)") == ["(id)"]
    assert get_keys("version", engine, "tuple()") == []
    replicated = "ReplicatedVersionedCollapsingMergeTree('/t', 'r', sign, version)"
    assert get_keys("id, version", replicated, "id") == ["id"]
    assert get_keys("id, version", "MergeTree", "(id, version)") == ["id", "version"]
