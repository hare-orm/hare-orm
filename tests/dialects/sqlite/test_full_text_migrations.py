"""A FullTextIndex through migrations on SQLite: created and filled, dropped, and kept in step
with its table through rebuilds, column renames, table renames and STRICT changes."""

import uuid

import pytest
import pytest_asyncio

from hare.dialects.sqlite.drivers.aiosqlite.client import AiosqliteClient
from hare.dialects.sqlite.indexes import FullTextIndex
from hare.dialects.sqlite.sqlite_table_options import SqliteTableOptions
from hare.exceptions import ConfigurationError
from hare.fields import CharField, IntField, TextField, UUIDField
from hare.migrations.operations import (
    AddField,
    AddIndex,
    AlterField,
    AlterModelOptions,
    AlterModelTable,
    CreateModel,
    DeleteModel,
    RemoveField,
    RemoveIndex,
    RenameField,
    RenameIndex,
    RenameModel,
)
from hare.migrations.state.state import State
from hare.migrations.state.state_apps import StateApps

APP_LABEL = "models"
TABLE = "fts_post"


class MigrationRunner:
    """Runs migration operations against an in-memory SQLite database."""

    def __init__(self, client: AiosqliteClient, *, collect_sql: bool = False) -> None:
        self.client = client
        self.state = State(models={}, apps=StateApps())
        self.editor = client.dialect.schema_editor_class(client, atomic=True, collect_sql=collect_sql)

    async def run(self, operation) -> None:
        await operation.run(APP_LABEL, self.state, dry_run=False, state_editor=self.editor)

    async def get_schema_names(self) -> set[str]:
        rows = await self.client.execute_dicts("SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")
        return {row["name"] for row in rows}

    async def get_full_text_tables(self) -> dict[str, str]:
        rows = await self.client.execute_dicts(
            "SELECT name, sql FROM sqlite_master WHERE type = 'table' AND sql LIKE 'CREATE VIRTUAL TABLE%'"
        )
        return {row["name"]: row["sql"] for row in rows}

    async def search(self, query: str) -> list[int]:
        (full_text_table,) = await self.get_full_text_tables()
        await self.client.execute(
            f'INSERT INTO "{full_text_table}" ("{full_text_table}") VALUES (\'integrity-check\')'
        )
        rows = await self.client.execute_dicts(
            f'SELECT rowid FROM "{full_text_table}" WHERE "{full_text_table}" MATCH ? ORDER BY rowid', [query]
        )
        return [row["rowid"] for row in rows]


@pytest_asyncio.fixture
async def runner():
    client = AiosqliteClient(file_path=":memory:", connection_alias="full_text_migrations")
    await client.create_connection(with_db=True)
    try:
        yield MigrationRunner(client)
    finally:
        await client.close()


def post_fields():
    return [
        ("id", IntField(primary_key=True)),
        ("title", CharField(max_length=100)),
        ("body", TextField(null=True)),
        ("views", IntField(default=0)),
    ]


async def create_posts(runner: MigrationRunner, indexes=()) -> None:
    await runner.run(
        CreateModel(name="Post", fields=post_fields(), options={"table": TABLE, "indexes": list(indexes)})
    )
    await runner.client.execute_script(
        f"INSERT INTO {TABLE} (id, title, body, views) VALUES (1, 'hare orm', 'fast queries', 1), "
        "(2, 'rabbit', 'a hare runs', 2)"
    )


@pytest.mark.asyncio
async def test_create_model_creates_the_index_with_its_triggers(runner):
    await create_posts(runner, [FullTextIndex(fields=("title", "body"), name="fts_post_text")])
    assert await runner.get_schema_names() >= {
        TABLE,
        "fts_post_text",
        "fts_post_text__insert",
        "fts_post_text__delete",
        "fts_post_text__update",
    }
    assert await runner.search("hare") == [1, 2]
    assert await runner.search("title : hare") == [1]


@pytest.mark.asyncio
async def test_add_index_fills_it_from_the_rows_there(runner):
    await create_posts(runner)
    await runner.run(AddIndex("Post", FullTextIndex(fields=("body",))))
    assert await runner.search("hare") == [2]
    await runner.client.execute_script(f"UPDATE {TABLE} SET body = 'no rabbits' WHERE id = 2")
    assert await runner.search("hare") == []


@pytest.mark.asyncio
async def test_remove_index_drops_the_table_and_its_triggers(runner):
    await create_posts(runner)
    schema_names = await runner.get_schema_names()
    await runner.run(AddIndex("Post", FullTextIndex(fields=("title", "body"), tokenizer="trigram")))
    assert len(await runner.get_schema_names()) > len(schema_names)
    await runner.run(RemoveIndex("Post", fields=["title", "body"]))
    assert await runner.get_schema_names() == schema_names


@pytest.mark.asyncio
async def test_rename_index_moves_it(runner):
    await create_posts(runner, [FullTextIndex(fields=("title",), name="fts_post_old")])
    await runner.run(RenameIndex("Post", new_name="fts_post_new", old_name="fts_post_old"))
    assert set(await runner.get_full_text_tables()) == {"fts_post_new"}
    assert await runner.search("hare") == [1]


@pytest.mark.asyncio
async def test_a_table_rebuild_keeps_the_index_in_step(runner):
    await create_posts(runner, [FullTextIndex(fields=("title", "body"))])
    await runner.run(RemoveField("Post", "views"))
    full_text_tables = await runner.get_full_text_tables()
    assert len(full_text_tables) == 1
    assert await runner.search("hare") == [1, 2]
    await runner.client.execute_script(f"INSERT INTO {TABLE} (id, title, body) VALUES (3, 'new hare', '')")
    assert await runner.search("hare") == [1, 2, 3]
    await runner.run(AddField("Post", "summary", TextField(null=True)))
    assert await runner.search("hare") == [1, 2, 3]


@pytest.mark.asyncio
async def test_removing_an_indexed_field_drops_its_index(runner):
    await create_posts(runner, [FullTextIndex(fields=("body",))])
    await runner.run(RemoveIndex("Post", fields=["body"]))
    await runner.run(RemoveField("Post", "body"))
    assert await runner.get_full_text_tables() == {}


@pytest.mark.asyncio
async def test_renaming_an_indexed_field_recreates_the_index_over_the_new_column(runner):
    await create_posts(runner, [FullTextIndex(fields=("title", "body"))])
    (old_table_name,) = await runner.get_full_text_tables()
    await runner.run(RenameField("Post", "body", "content"))
    full_text_tables = await runner.get_full_text_tables()
    assert old_table_name not in full_text_tables
    (new_table_sql,) = full_text_tables.values()
    assert '"content"' in new_table_sql
    assert await runner.search("content : hare") == [2]
    await runner.client.execute_script(f"UPDATE {TABLE} SET content = 'hare again' WHERE id = 1")
    assert await runner.search("content : hare") == [1, 2]


@pytest.mark.asyncio
@pytest.mark.parametrize("null", [True, False], ids=["column renamed", "column renamed and altered"])
async def test_altering_an_indexed_field_column_recreates_the_index(runner, null):
    await create_posts(runner, [FullTextIndex(fields=("title", "body"))])
    if not null:
        await runner.client.execute_script(f"UPDATE {TABLE} SET body = '' WHERE body IS NULL")
    (old_table_name,) = await runner.get_full_text_tables()
    await runner.run(AlterField("Post", "body", TextField(null=null, source_field="body_text")))
    full_text_tables = await runner.get_full_text_tables()
    assert old_table_name not in full_text_tables
    assert '"body_text"' in next(iter(full_text_tables.values()))
    assert await runner.search("body_text : hare") == [2]


@pytest.mark.asyncio
@pytest.mark.parametrize("rename", ["model", "table"])
async def test_renaming_the_table_moves_the_index_onto_it(runner, rename):
    await create_posts(runner, [FullTextIndex(fields=("title",))])
    if rename == "model":
        await runner.run(AlterModelTable("Post", "fts_article"))
        await runner.run(RenameModel("Post", "Article"))
    else:
        await runner.run(AlterModelTable("Post", "fts_article"))
    (full_text_sql,) = (await runner.get_full_text_tables()).values()
    assert "content='fts_article'" in full_text_sql
    assert await runner.search("hare") == [1]
    await runner.client.execute_script("INSERT INTO fts_article (id, title, views) VALUES (5, 'hare five', 0)")
    assert await runner.search("hare") == [1, 5]


@pytest.mark.asyncio
async def test_delete_model_drops_the_index(runner):
    await create_posts(runner, [FullTextIndex(fields=("title",))])
    await runner.run(DeleteModel("Post"))
    assert await runner.get_schema_names() == set()


@pytest.mark.asyncio
@pytest.mark.skipif(
    not AiosqliteClient.features.supports_strict_tables, reason="the SQLite library has no STRICT tables"
)
async def test_making_the_table_strict_keeps_the_index(runner):
    await create_posts(runner, [FullTextIndex(fields=("title", "body"))])
    await runner.run(AlterModelOptions("Post", {"table_options": [SqliteTableOptions(strict=True)]}))
    rows = await runner.client.execute_dicts("SELECT sql FROM sqlite_master WHERE name = ?", [TABLE])
    assert rows[0]["sql"].rstrip().endswith("STRICT")
    assert await runner.search("hare") == [1, 2]


@pytest.mark.asyncio
async def test_collected_sql_creates_the_index():
    client = AiosqliteClient(file_path=":memory:", connection_alias="full_text_sql")
    await client.create_connection(with_db=True)
    try:
        runner = MigrationRunner(client, collect_sql=True)
        await runner.run(
            CreateModel(
                name="Post",
                fields=post_fields(),
                options={"table": TABLE, "indexes": [FullTextIndex(fields=("title",), name="fts_post_title")]},
            )
        )
        collected_sql = "\n".join(runner.editor.collected_sql)
    finally:
        await client.close()
    assert 'CREATE VIRTUAL TABLE "fts_post_title" USING fts5("title"' in collected_sql
    assert 'CREATE TRIGGER "fts_post_title__update" AFTER UPDATE OF "id", "title" ON "fts_post"' in collected_sql
    assert """INSERT INTO "fts_post_title" ("fts_post_title") VALUES ('rebuild')""" in collected_sql


@pytest.mark.asyncio
async def test_a_model_without_an_integer_primary_key_is_refused(runner):
    with pytest.raises(ConfigurationError, match="integer primary key"):
        await runner.run(
            CreateModel(
                name="Note",
                fields=[("id", UUIDField(primary_key=True, default=uuid.uuid4)), ("text", TextField())],
                options={"table": "fts_note", "indexes": [FullTextIndex(fields=("text",))]},
            )
        )
