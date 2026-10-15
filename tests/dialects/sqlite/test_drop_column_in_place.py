"""SQLite drops a removed field's column with ALTER TABLE ... DROP COLUMN when nothing - in the models
or in the database - keeps it from doing so, and rebuilds the table otherwise."""

import pytest

from hare.ddl.constraints.check_constraint import CheckConstraint
from hare.ddl.indexes.index import Index
from hare.dialects.sqlite.drivers.aiosqlite.client import AiosqliteClient
from hare.fields import CharField, IntField
from hare.migrations.operations import CreateModel, RemoveField
from hare.migrations.state.state import State
from hare.migrations.state.state_apps import StateApps
from hare.query.expressions import Q

TABLE = "drop_book"


class RecordingEditor:
    """Runs the migration operations against an in-memory SQLite database and records the SQL."""

    def __init__(self, client: AiosqliteClient, *, supports_drop_column: bool = True) -> None:
        if not supports_drop_column:
            client.features = client.features.replace(supports_drop_column=False)
        self.editor = client.dialect.schema_editor_class(client, atomic=True, collect_sql=False)
        self.statements: list[str] = []
        run_sql = self.editor.run_sql

        async def record_and_run(sql: str) -> None:
            self.statements.append(sql)
            await run_sql(sql)

        self.editor.run_sql = record_and_run

    @property
    def dropped_in_place(self) -> bool:
        return any("DROP COLUMN" in statement for statement in self.statements)

    @property
    def rebuilt(self) -> bool:
        return any(f"new__{TABLE}" in statement for statement in self.statements)


async def remove_pages(*, pages_field=None, options=None, database_sql: str = "", supports_drop_column=True):
    client = AiosqliteClient(file_path=":memory:", connection_alias="drop_column")
    await client.create_connection(with_db=True)
    try:
        recorder = RecordingEditor(client, supports_drop_column=supports_drop_column)
        state = State(models={}, apps=StateApps())
        await CreateModel(
            name="Book",
            fields=[
                ("id", IntField(primary_key=True)),
                ("title", CharField(max_length=20, null=True)),
                ("pages", pages_field or IntField(null=True)),
            ],
            options={"table": TABLE, **(options or {})},
        ).run("models", state, dry_run=False, state_editor=recorder.editor)
        if database_sql:
            await client.execute_script(database_sql)
        await client.execute_script(f"INSERT INTO {TABLE} (id, title, pages) VALUES (1, 'kept', 10)")
        recorder.statements.clear()
        await RemoveField(model_name="Book", name="pages").run(
            "models", state, dry_run=False, state_editor=recorder.editor
        )
        _, columns = await client.execute(f"PRAGMA table_info({TABLE})")
        _, rows = await client.execute(f"SELECT id, title FROM {TABLE}")
    finally:
        await client.close()
    assert [column["name"] for column in columns] == ["id", "title"]
    assert [(row["id"], row["title"]) for row in rows] == [(1, "kept")]
    return recorder


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "options", [None, {"indexes": [Index(fields=("title",))]}], ids=["no index", "index on another column"]
)
async def test_a_plain_column_is_dropped_in_place(options):
    recorder = await remove_pages(options=options)
    assert recorder.dropped_in_place
    assert not recorder.rebuilt


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("pages_field", "options", "database_sql"),
    [
        pytest.param(IntField(null=True, db_index=True), None, "", id="indexed field"),
        pytest.param(IntField(null=True, unique=True), None, "", id="unique field"),
        pytest.param(
            None,
            {"constraints": [CheckConstraint(check=Q(title__isnull=False), name="drop_book_title_set")]},
            "",
            id="model with a CHECK",
        ),
        pytest.param(None, None, f"CREATE INDEX drop_book_pages ON {TABLE} (pages)", id="index only in the database"),
        pytest.param(
            None, None, f"CREATE INDEX drop_book_title ON {TABLE} (title) WHERE title IS NOT NULL", id="partial index"
        ),
        pytest.param(
            None,
            None,
            f"CREATE TRIGGER drop_book_touch AFTER UPDATE ON {TABLE} BEGIN SELECT 1; END",
            id="trigger in the database",
        ),
    ],
)
async def test_a_column_sqlite_cannot_drop_rebuilds_the_table(pages_field, options, database_sql):
    recorder = await remove_pages(pages_field=pages_field, options=options, database_sql=database_sql)
    assert recorder.rebuilt
    assert not recorder.dropped_in_place


@pytest.mark.asyncio
async def test_a_view_naming_the_column_keeps_it_from_being_dropped_in_place():
    client = AiosqliteClient(file_path=":memory:", connection_alias="drop_column")
    await client.create_connection(with_db=True)
    try:
        recorder = RecordingEditor(client)
        state = State(models={}, apps=StateApps())
        await CreateModel(
            name="Book",
            fields=[("id", IntField(primary_key=True)), ("pages", IntField(null=True))],
            options={"table": TABLE},
        ).run("models", state, dry_run=False, state_editor=recorder.editor)
        await client.execute_script(f"CREATE VIEW drop_book_pages_view AS SELECT pages FROM {TABLE}")
        assert not await recorder.editor._database_allows_drop_column(TABLE, None, "pages")
        await client.execute_script("DROP VIEW drop_book_pages_view")
        assert await recorder.editor._database_allows_drop_column(TABLE, None, "pages")
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_an_old_sqlite_rebuilds_the_table():
    recorder = await remove_pages(supports_drop_column=False)
    assert recorder.rebuilt
    assert not recorder.dropped_in_place
