"""ClickHouse's data skipping indexes - each type with its arguments and granularity: created with
the table, read back by the introspector, rebuilt by inspectdb, compared by drift, added and removed
by a migration, and refused on another database."""

import pytest

from hare.dialects.clickhouse.indexes import (
    BloomFilterIndex,
    MinMaxIndex,
    NgramBloomFilterIndex,
    SetIndex,
    TokenBloomFilterIndex,
)
from hare.exceptions import ConfigurationError, UnSupportedError
from hare.inspectdb.generation.model_source_generator import ModelSourceGenerator
from hare.inspectdb.introspection.database_catalog import DatabaseCatalog
from hare.migrations.drift import detect_drift
from hare.migrations.operations import AddIndex, RemoveIndex
from hare.migrations.state.model_state import ModelState
from hare.migrations.state.state import State
from hare.migrations.state.state_apps import StateApps
from tests.dialects.clickhouse.models import Article


async def get_index_types(connection):
    rows = await connection.execute_dicts(
        "SELECT name, type_full, granularity FROM system.data_skipping_indices "
        "WHERE database = currentDatabase() AND table = 'article' ORDER BY name"
    )
    return {row["name"]: (row["type_full"], row["granularity"]) for row in rows}


@pytest.mark.asyncio
async def test_indexes_are_created_with_their_type(clickhouse_db):
    assert await get_index_types(Article._meta.connection) == {
        "article_body_ngram_idx": ("ngrambf_v1(4, 512, 3, 0)", 1),
        "article_body_token_idx": ("tokenbf_v1(256, 2, 0)", 2),
        "article_plain_idx": ("minmax", 1),
        "article_tags_idx": ("bloom_filter(0.01)", 1),
        "article_title_idx": ("set(100)", 1),
        "article_views_idx": ("minmax", 4),
    }


@pytest.mark.asyncio
async def test_indexes_are_read_back_and_drift_finds_none(clickhouse_db):
    connection = Article._meta.connection
    table = await DatabaseCatalog.inspect_table(connection, "article")
    assert {index.name: (index.index_type, index.storage_parameters) for index in table.indexes} == {
        "article_body_ngram_idx": (
            "ngrambf_v1",
            {"granularity": "1", "ngram_size": "4", "filter_size": "512", "hash_functions": "3", "seed": "0"},
        ),
        "article_body_token_idx": (
            "tokenbf_v1",
            {"granularity": "2", "filter_size": "256", "hash_functions": "2", "seed": "0"},
        ),
        "article_plain_idx": ("", {}),
        "article_tags_idx": ("bloom_filter", {"granularity": "1", "false_positive": "0.01"}),
        "article_title_idx": ("set", {"granularity": "1", "max_rows": "100"}),
        "article_views_idx": ("minmax", {"granularity": "4"}),
    }
    source = ModelSourceGenerator.generate_model_source(table, connection.dialect.name)
    compile(source, "<generated>", "exec")
    for expected in (
        "MinMaxIndex(fields=['views'], name='article_views_idx', granularity=4)",
        "SetIndex(fields=['title'], name='article_title_idx', max_rows=100)",
        "BloomFilterIndex(fields=['tags'], name='article_tags_idx', false_positive=0.01)",
        "NgramBloomFilterIndex(fields=['body'], name='article_body_ngram_idx', filter_size=512, hash_functions=3, "
        "ngram_size=4)",
        "TokenBloomFilterIndex(fields=['body'], name='article_body_token_idx', granularity=2)",
    ):
        assert expected in source, (expected, source)
    state = State(models={}, apps=StateApps())
    state.models[("models", "Article")] = ModelState.make_from_model("models", Article)
    drift = await detect_drift(connection, state, ["models"])
    assert [operation for operation in drift.operations if getattr(operation, "model_name", "") == "Article"] == []


@pytest.mark.asyncio
async def test_a_migration_adds_and_removes_an_index(clickhouse_db):
    connection = Article._meta.connection
    editor = connection.dialect.schema_editor_class(connection, atomic=True, collect_sql=False)
    state = State(models={}, apps=StateApps())
    state.models[("models", "Article")] = ModelState.make_from_model("models", Article)
    index = SetIndex(fields=["views"], max_rows=10, granularity=3, name="article_views_set_idx")
    await AddIndex(model_name="Article", index=index).run("models", state, dry_run=False, state_editor=editor)
    assert (await get_index_types(connection))["article_views_set_idx"] == ("set(10)", 3)
    await RemoveIndex(model_name="Article", name="article_views_set_idx").run(
        "models", state, dry_run=False, state_editor=editor
    )
    assert "article_views_set_idx" not in await get_index_types(connection)


@pytest.mark.asyncio
async def test_an_index_skips_granules(clickhouse_db):
    await Article.objects.bulk_create(
        [
            Article(id=number, title=f"t{number % 3}", body=f"word{number} text", views=number, tags=["a"])
            for number in range(1, 30)
        ]
    )
    assert await Article.objects.filter(title="t1", views__gt=20).order_by("id").values_list("id", flat=True) == [
        22,
        25,
        28,
    ]
    assert await Article.objects.filter(body__contains="word17").values_list("id", flat=True) == [17]


def test_arguments_are_checked_and_other_databases_refused():
    from hare.dialects.sqlite.constants import SQLITE_DIALECT

    for index_class, arguments in (
        (MinMaxIndex, {"granularity": 0}),
        (SetIndex, {"max_rows": -1}),
        (BloomFilterIndex, {"false_positive": 1}),
        (BloomFilterIndex, {"false_positive": True}),
        (NgramBloomFilterIndex, {"ngram_size": 0}),
        (TokenBloomFilterIndex, {"hash_functions": "2"}),
        (SetIndex, {"include": ["title"]}),
    ):
        with pytest.raises((ConfigurationError, UnSupportedError)):
            index_class(fields=["title"], **arguments)
    with pytest.raises(UnSupportedError, match="index of ClickHouse"):
        SetIndex(fields=["title"]).raise_if_unsupported(SQLITE_DIALECT)
