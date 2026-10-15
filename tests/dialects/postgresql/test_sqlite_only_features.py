"""SQLite's FTS5 FullTextIndex and SearchRank's weights by field on PostgreSQL: each raises
UnSupportedError before any SQL is sent.
SqliteTableOptions(strict=True) is SQLite's entry of Meta.table_options - PostgreSQL ignores it,
and ``__search`` stays PostgreSQL's own text search."""

import os

import pytest
import pytest_asyncio

from hare import fields
from hare.contrib.test.isolated_contexts import hare_test_context
from hare.dialects.sqlite.indexes import FullTextIndex
from hare.exceptions import UnSupportedError
from hare.migrations.operations import AddIndex, CreateModel, RemoveIndex
from hare.migrations.state.state import State
from hare.migrations.state.state_apps import StateApps
from hare.search import SearchRank
from tests.dialects.postgresql.conftest import skip_if_not_postgres
from tests.dialects.postgresql.models_sqlite_features import SqliteFeatureDocument

APP_LABEL = "models"


@pytest_asyncio.fixture
async def postgres_db():
    skip_if_not_postgres()
    async with hare_test_context(
        ["tests.dialects.postgresql.models_sqlite_features"], db_url=os.environ["HARE_TEST_DB"]
    ) as context:
        yield context


class RecordingEditor:
    """A schema editor of the connection that records every statement it runs."""

    def __init__(self, connection) -> None:
        self.editor = connection.dialect.schema_editor_class(connection, atomic=True, collect_sql=False)
        self.statements: list[str] = []
        run_sql = self.editor.run_sql

        async def record_and_run(sql: str) -> None:
            self.statements.append(sql)
            await run_sql(sql)

        self.editor.run_sql = record_and_run


@pytest.mark.asyncio
async def test_a_strict_sqlite_table_option_is_ignored(postgres_db):
    document = await SqliteFeatureDocument.objects.create(title="hare orm", body="fast")
    assert await SqliteFeatureDocument.objects.filter(id=document.id).count() == 1
    assert not postgres_db.get_connection().features.supports_strict_tables


@pytest.mark.asyncio
async def test_search_stays_postgresql_text_search(postgres_db):
    document = await SqliteFeatureDocument.objects.create(title="Hare ORM", body="an async mapper")
    assert await SqliteFeatureDocument.objects.filter(body__search="mapper").values_list("id", flat=True) == [
        document.id
    ]


@pytest.mark.asyncio
async def test_sqlite_only_search_arguments_are_refused_before_sql(postgres_db):
    connection = postgres_db.get_connection()
    assert not connection.features.supports_full_text_index
    with pytest.raises(UnSupportedError, match="supports_full_text_index"):
        await SqliteFeatureDocument.objects.annotate(rank=SearchRank("title", "hare", weights={"title": 2.0}))


@pytest.mark.asyncio
async def test_a_full_text_index_is_refused_before_sql(postgres_db):
    recorder = RecordingEditor(postgres_db.get_connection())
    state = State(models={}, apps=StateApps())
    with pytest.raises(UnSupportedError, match="supports_full_text_index"):
        await CreateModel(
            name="Indexed",
            fields=[("id", fields.IntField(primary_key=True)), ("title", fields.TextField())],
            options={"table": "pg_full_text_indexed", "indexes": [FullTextIndex(fields=("title",))]},
        ).run(APP_LABEL, state, dry_run=False, state_editor=recorder.editor)
    assert not any("pg_full_text_indexed" in statement for statement in recorder.statements)
    state = State(models={}, apps=StateApps())
    await CreateModel(
        name="Plain",
        fields=[("id", fields.IntField(primary_key=True)), ("title", fields.TextField())],
        options={"table": "pg_full_text_plain"},
    ).run(APP_LABEL, state, dry_run=False, state_editor=recorder.editor)
    recorder.statements.clear()
    with pytest.raises(UnSupportedError, match="supports_full_text_index"):
        await AddIndex("Plain", FullTextIndex(fields=("title",))).run(
            APP_LABEL, state, dry_run=False, state_editor=recorder.editor
        )
    assert recorder.statements == []
    (plain_state,) = state.models.values()
    plain_state.options["indexes"] = [FullTextIndex(fields=("title",), name="plain_text")]
    with pytest.raises(UnSupportedError, match="supports_full_text_index"):
        await RemoveIndex("Plain", name="plain_text").run(
            APP_LABEL, state, dry_run=False, state_editor=recorder.editor
        )
    assert recorder.statements == []
