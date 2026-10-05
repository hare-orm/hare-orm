"""A FullTextIndex through the migration autodetector, hare drift and inspectdb on SQLite: its FTS5
table is read back as the index of its table, its triggers and shadow tables as part of it."""

import pytest
import pytest_asyncio

from hare import fields
from hare.contrib.test import hare_test_context
from hare.dialects.sqlite.indexes import FullTextIndex
from hare.dialects.sqlite.sqlite_introspector import SqliteIntrospector
from hare.inspectdb import SchemaInspector
from hare.migrations.drift import detect_drift
from tests.migrations.test_round_trip_real_db import APP_LABEL, RoundTrip, build_live_state, build_model


@pytest_asyncio.fixture
async def round_trip():
    async with hare_test_context(
        ["tests.dialects.sqlite.models_full_text"], db_url="sqlite+aiosqlite://:memory:"
    ) as context:
        yield RoundTrip(context.get_connection())


def build_post(indexes: list[FullTextIndex]) -> type:
    return build_model(
        "Post",
        "drift_post",
        {"title": fields.CharField(max_length=100), "body": fields.TextField(null=True)},
        {"indexes": indexes},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "index",
    [
        FullTextIndex(fields=("title", "body")),
        FullTextIndex(fields=("body",), tokenizer="porter unicode61 remove_diacritics 2"),
        FullTextIndex(fields=("title",), name="drift_post_titles", tokenizer="trigram"),
    ],
    ids=["unnamed", "tokenizer", "named"],
)
async def test_a_migrated_index_has_no_drift(round_trip, index):
    post = build_post([index])
    operations = await round_trip.migrate_to(post)
    assert [type(operation).__name__ for operation in operations] == ["CreateModel"]
    drift = await detect_drift(round_trip.connection, build_live_state(post), [APP_LABEL])
    assert drift.operations == []
    assert round_trip.get_pending_operations(post) == []


@pytest.mark.asyncio
async def test_a_changed_or_missing_index_is_drift(round_trip):
    post = build_post([FullTextIndex(fields=("title",), tokenizer="trigram")])
    await round_trip.migrate_to(post)
    changed = build_post([FullTextIndex(fields=("title",))])
    drift = await detect_drift(round_trip.connection, build_live_state(changed), [APP_LABEL])
    assert sorted(type(operation).__name__ for operation in drift.operations) == ["AddIndex", "RemoveIndex"]
    operations = await round_trip.migrate_to(changed)
    assert sorted(type(operation).__name__ for operation in operations) == ["AddIndex", "RemoveIndex"]
    assert (await detect_drift(round_trip.connection, build_live_state(changed), [APP_LABEL])).operations == []
    unindexed = build_post([])
    await round_trip.migrate_to(unindexed)
    assert (await detect_drift(round_trip.connection, build_live_state(unindexed), [APP_LABEL])).operations == []


@pytest.mark.asyncio
async def test_inspectdb_reads_the_index_and_skips_its_tables(round_trip):
    await round_trip.migrate_to(build_post([FullTextIndex(fields=("title", "body"), tokenizer="porter")]))
    table_names = await SqliteIntrospector.fetch_table_names(round_trip.connection, "main", False)
    assert "drift_post" in table_names
    assert not [name for name in table_names if name.startswith("idx_")]
    source = await SchemaInspector.inspect(round_trip.connection, ["drift_post"])
    assert "from hare.dialects.sqlite.indexes import FullTextIndex" in source
    assert "FullTextIndex(fields=['title', 'body']" in source
    assert "tokenizer='porter'" in source
    assert "Trigger(" not in source


def test_a_virtual_table_other_than_a_full_text_index_is_no_index():
    assert (
        SqliteIntrospector.get_full_text_index_info("post", "post_fts", "CREATE VIRTUAL TABLE t USING rtree(id, x)")
        is None
    )
    assert (
        SqliteIntrospector.get_full_text_index_info(
            "post", "post_fts", "CREATE VIRTUAL TABLE t USING fts5(title, body)"
        )
        is None
    )
    assert (
        SqliteIntrospector.get_full_text_index_info(
            "post",
            "post_fts",
            "CREATE VIRTUAL TABLE t USING fts5(title UNINDEXED, content='post', content_rowid='id')",
        )
        is None
    )
    index_info = SqliteIntrospector.get_full_text_index_info(
        "post",
        "post_fts",
        """CREATE VIRTUAL TABLE "post_fts" USING fts5("ti""tle", [body], content='post', content_rowid='id', """
        """tokenize='porter, ''odd''')""",
    )
    assert index_info is not None
    assert index_info.columns == ['ti"tle', "body"]
    assert index_info.storage_parameters == {"tokenizer": "porter, 'odd'"}
