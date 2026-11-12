"""Engine declarations distinguish column dependencies, literals and replication paths."""

import pytest

from hare import Model, fields
from hare.classes.comparable_values import ComparableValues
from hare.ddl.raw_sql_term import RawSQLTerm
from hare.dialects.clickhouse.clickhouse_engine import ClickhouseEngine
from hare.dialects.clickhouse.clickhouse_table_options import ClickhouseTableOptions
from hare.dialects.clickhouse.constants import CLICKHOUSE_DIALECT
from hare.dialects.clickhouse.introspection.clickhouse_catalog_engine import ClickhouseCatalogEngine
from hare.dialects.clickhouse.introspection.clickhouse_catalog_identities import ClickhouseCatalogIdentities
from hare.exceptions import ConfigurationError, UnSupportedError
from hare.inspectdb.enums import SchemaPartType
from hare.inspectdb.introspection.schema_part import SchemaPart
from hare.query.expressions import F

pytestmark = pytest.mark.database_independent


class Book(Model):
    id = fields.IntField(primary_key=True, generated=False)
    version = fields.IntField(source_field="revision number")
    next_version = fields.IntField(source_field="next revision")


@pytest.mark.parametrize("prefix", ["", "Replicated", "Shared"])
@pytest.mark.parametrize(
    "family,arguments,columns",
    [
        ("MergeTree", (), set()),
        ("ReplacingMergeTree", (F("version"),), {"revision number"}),
        ("VersionedCollapsingMergeTree", (F("version"), F("next_version")), {"revision number", "next revision"}),
        ("SummingMergeTree", ((F("version"), F("next_version")),), {"revision number", "next revision"}),
        ("GraphiteMergeTree", ("version",), set()),
    ],
)
def test_engine_dependencies_and_renames_preserve_literal_arguments(prefix, family, arguments, columns):
    engine = ClickhouseEngine(prefix + family, *arguments)
    assert engine.get_column_names(Book, CLICKHOUSE_DIALECT) == columns
    renamed = engine.with_renamed_field("version", "next_version")
    expected = (columns - {"revision number"}) | ({"next revision"} if "revision number" in columns else set())
    assert renamed.get_column_names(Book, CLICKHOUSE_DIALECT) == expected
    assert engine.get_column_names(Book, CLICKHOUSE_DIALECT) == columns
    if family == "GraphiteMergeTree":
        assert renamed.get_sql(Book, CLICKHOUSE_DIALECT) == f"{prefix}GraphiteMergeTree('version')"
    path, args, options = engine.deconstruct()
    assert path.endswith("ClickhouseEngine")
    assert ComparableValues.are_equal(ClickhouseEngine(*args, **options), engine)


@pytest.mark.parametrize("prefix", ["Replicated", "Shared"])
@pytest.mark.parametrize("keeper", ["", "aux:"])
def test_shadow_replica_paths_are_isolated_and_catalog_identity_is_restored(prefix, keeper):
    engine = ClickhouseEngine(
        prefix + "ReplacingMergeTree",
        F("version"),
        replica_path=keeper + "/tables/{uuid}/{shard}",
        replica_name="{replica}",
    )
    shadow = engine.for_shadow("shadow")
    assert shadow.replica_path == keeper + "/shadow/tables/{uuid}/{shard}"
    mapper = ClickhouseCatalogIdentities({}, "shadow", CLICKHOUSE_DIALECT.literals)
    assert mapper.map_engine(shadow.get_sql(Book, CLICKHOUSE_DIALECT)) == engine.get_sql(Book, CLICKHOUSE_DIALECT)


def test_catalog_unknown_arguments_remain_raw_and_are_not_guessed_as_columns():
    sql = "CustomEngine(version, 'version')"
    engine = ClickhouseCatalogEngine.get(sql)
    assert isinstance(engine, ClickhouseEngine)
    assert engine.get_sql(Book, CLICKHOUSE_DIALECT) == sql
    assert engine.get_column_names(Book, CLICKHOUSE_DIALECT) == frozenset()
    explicit = ClickhouseEngine("CustomEngine", read_fields=("version",), sql=RawSQLTerm(sql))
    assert explicit.get_column_names(Book, CLICKHOUSE_DIALECT) == {"revision number"}
    assert explicit.with_renamed_field("version", "next_version").get_sql(Book, CLICKHOUSE_DIALECT) == sql
    with pytest.raises(UnSupportedError, match="shadow"):
        ClickhouseEngine("ReplicatedMergeTree", sql=RawSQLTerm("ReplicatedMergeTree()")).for_shadow("shadow")


def test_bare_engine_names_have_no_arguments_and_default_options_are_omitted():
    assert ClickhouseTableOptions(engine="MergeTree") == ClickhouseTableOptions()
    assert ClickhouseTableOptions.from_observed({"engine": ClickhouseCatalogEngine.get("MergeTree")}) is None
    with pytest.raises(ConfigurationError, match="ClickhouseEngine"):
        ClickhouseTableOptions(engine="ReplacingMergeTree(version)")


@pytest.mark.parametrize("path", ["/tables/{uuid}/{shard}", "/tables/{database}/{table}"])
@pytest.mark.parametrize("prefix", ["Replicated", "Shared"])
def test_safe_server_defaults_stay_implicit_in_shadow_engines(path, prefix):
    engine = ClickhouseEngine(prefix + "MergeTree")
    assert engine.for_shadow("shadow", {"default_replica_path": path}) is engine
    with pytest.raises(UnSupportedError, match="captured"):
        engine.for_shadow("shadow")
    with pytest.raises(UnSupportedError, match="default_replica_path"):
        engine.for_shadow("shadow", {"default_replica_path": "/tables/{table}/{shard}"})


def test_distributed_engine_keeps_names_literal_and_binds_the_sharding_expression():
    engine = ClickhouseTableOptions(distributed_over="books", sharding_key="version").get_distributed_engine(
        Book, "cluster's", "books"
    )
    assert engine.arguments[:3] == ("cluster's", RawSQLTerm("currentDatabase()"), "books")
    assert engine.get_column_names(Book, CLICKHOUSE_DIALECT) == {"revision number"}
    assert engine.get_sql(Book, CLICKHOUSE_DIALECT).endswith(', "revision number")')


def test_catalog_column_names_are_mapped_to_generated_fields_without_touching_literals():
    engine = ClickhouseCatalogEngine.get("ReplacingMergeTree(`revision number`)")
    options = ClickhouseTableOptions(engine=engine).with_field_names({"revision number": "version"})
    assert options.get_engine().get_sql(Book, CLICKHOUSE_DIALECT) == 'ReplacingMergeTree("revision number")'
    literal = ClickhouseEngine("GraphiteMergeTree", "revision number")
    assert literal.with_field_names({"revision number": "version"}).arguments == ("revision number",)


def test_catalog_view_storage_identity_does_not_rewrite_select_literals():
    query = (
        "CREATE MATERIALIZED VIEW shadowdb.summary "
        "ENGINE = ReplicatedMergeTree('/shadow/tables/{uuid}', '{replica}') ORDER BY id "
        "AS SELECT id, '/shadow/tables/literal' AS title FROM shadowdb.book"
    )
    mapper = ClickhouseCatalogIdentities({"shadowdb": "live"}, "shadow", CLICKHOUSE_DIALECT.literals)
    part = SchemaPart(SchemaPartType.MATERIALIZED_VIEW, "", "summary", {"query": query})
    mapped = mapper.map_part(part)
    assert "ReplicatedMergeTree('/tables/{uuid}', '{replica}')" in mapped.definition["query"]
    assert "'/shadow/tables/literal'" in mapped.definition["query"]
    assert 'FROM "live".book' in mapped.definition["query"]


@pytest.mark.parametrize(
    "source", ["CLICKHOUSE(DB 'shadowdb' TABLE 'book')", "CLICKHOUSE(TABLE 'book' DB 'shadowdb')"]
)
def test_catalog_dictionary_maps_only_the_local_source_database(source):
    query = f"CREATE DICTIONARY shadowdb.titles (id UInt64, title String DEFAULT 'shadowdb') SOURCE({source})"
    mapper = ClickhouseCatalogIdentities({"shadowdb": "live"}, "shadow", CLICKHOUSE_DIALECT.literals)
    part = SchemaPart(SchemaPartType.DICTIONARY, "", "titles", {"query": query})
    mapped = mapper.map_part(part).definition["query"]
    assert 'CREATE DICTIONARY "live".titles' in mapped
    assert "DB 'live'" in mapped
    assert "DEFAULT 'shadowdb'" in mapped
    assert part.definition["query"] == query


@pytest.mark.parametrize(
    "source",
    [
        "CLICKHOUSE(HOST 'remote' DB 'shadowdb' TABLE 'book')",
        "MYSQL(DB 'shadowdb' TABLE 'book')",
        "CLICKHOUSE(QUERY 'SELECT * FROM shadowdb.book WHERE title = DB')",
    ],
)
def test_catalog_dictionary_preserves_remote_sources_and_query_literals(source):
    mapper = ClickhouseCatalogIdentities({"shadowdb": "live"}, "shadow", CLICKHOUSE_DIALECT.literals)
    query = f"CREATE DICTIONARY titles (id UInt64) SOURCE({source})"
    assert mapper.map_dictionary_source(query) == query
