"""Prepared schema work has the same statements in preview and execution."""

import asyncio
import sqlite3
from unittest.mock import AsyncMock, Mock

import pytest

from hare import Model, fields
from hare.ddl.indexes.index import Index
from hare.dialects.base.results import StatementResult
from hare.dialects.base.schema.plans.enums import SchemaEffect
from hare.dialects.base.schema.plans.schema_change_plan import SchemaChangePlan
from hare.dialects.base.schema.plans.schema_cleanup import SchemaCleanup
from hare.dialects.base.schema.plans.schema_statement import SchemaStatement
from hare.dialects.base.schema.plans.stored_value_rewrite import StoredValueRewrite
from hare.dialects.postgresql.partitioning.hash_partitioning import HashPartitioning
from hare.dialects.postgresql.postgresql_table_options import PostgresqlTableOptions
from hare.dialects.sqlite.schema.tables.sqlite_legacy_rename import SqliteLegacyRename
from hare.dialects.sqlite.sqlite_table_options import SqliteTableOptions
from hare.exceptions import ConfigurationError
from hare.migrations.exceptions import FieldNarrowingDataLossError
from hare.migrations.migration import Migration
from hare.migrations.operations import AddField, AlterField, AlterModelOptions, CreateModel
from hare.migrations.safety import MigrationRiskCode, MigrationSafetyChecker
from hare.migrations.state.state import State
from hare.migrations.state.state_apps import StateApps
from tests.utils.fake_client import FakeClient


@pytest.mark.database_independent
@pytest.mark.parametrize("failure", ["conversion", "write"])
@pytest.mark.parametrize("completed_batches", [0, 1])
@pytest.mark.asyncio
async def test_stored_rewrite_records_write_attempts_after_conversion(monkeypatch, failure, completed_batches):
    import hare.dialects.base.schema.plans.stored_value_rewrite as rewrite_module

    monkeypatch.setattr(rewrite_module, "STORED_VALUE_REWRITE_BATCH_SIZE", 1)
    client = FakeClient("sqlite")
    editor = client.dialect.schema_editor_class(client, atomic=False)
    written = []

    async def read(*args, **kwargs):
        return StatementResult(1, [{"id": len(written), "value": len(written)}])

    async def write(sql, parameters, **kwargs):
        if failure == "write" and len(written) == completed_batches:
            raise ConnectionError("batch write failed")
        written.extend(parameters)

    def convert(value):
        if failure == "conversion" and value == completed_batches:
            raise ValueError("conversion failed")
        return value + 10

    client.execute = read
    client.execute_many = write
    step = StoredValueRewrite("SELECT", "UPDATE", "value", ("id",), convert, "convert values")
    with pytest.raises(ValueError if failure == "conversion" else ConnectionError):
        await step.execute(editor)
    assert written == ([[10, 0]] if completed_batches else [])
    assert editor.run_statement_count == completed_batches + (failure == "write")


@pytest.mark.database_independent
@pytest.mark.parametrize("partitioned", [False, True])
@pytest.mark.parametrize("unique", [False, True])
@pytest.mark.asyncio
async def test_concurrent_index_plan_matches_execution(partitioned, unique):
    state = State()
    options = (
        [PostgresqlTableOptions(partitioning=HashPartitioning(fields=("id",), partition_count=2))]
        if partitioned
        else []
    )
    CreateModel(
        "Book",
        [("id", fields.IntField(primary_key=True, generated=False))],
        options={"table": "book", "table_options": options},
    ).state_forward("models", state)
    model = state.apps.get_model("models.Book")
    client = FakeClient("postgresql")
    editor = client.dialect.schema_editor_class(client, atomic=False)
    index = Index(fields=("id",), name="book_index", unique=unique)
    plan = editor.get_add_index_plan(model, index, concurrently=True)
    preview = client.dialect.schema_editor_class(client, collect_sql=True, atomic=False)
    await plan.execute(preview)
    statements = preview.collected_sql
    assert len(statements) == (5 if partitioned else 1)
    prefix = "CREATE UNIQUE INDEX CONCURRENTLY" if unique else "CREATE INDEX CONCURRENTLY"
    assert sum(sql.startswith(prefix) for sql in statements) == (2 if partitioned else 1)
    assert sum("ATTACH PARTITION" in sql for sql in statements) == (2 if partitioned else 0)

    editor.run_sql = AsyncMock()
    await plan.execute(editor)
    assert [call.args[0] for call in editor.run_sql.await_args_list] == statements

    removal = editor.get_remove_index_plan(model, index, concurrently=True)
    preview.collected_sql.clear()
    await removal.execute(preview)
    assert len(preview.collected_sql) == 1
    assert ("CONCURRENTLY" in preview.collected_sql[0]) is not partitioned


@pytest.mark.database_independent
@pytest.mark.parametrize(
    ("dialect", "options", "rebuild"),
    [
        ("sqlite", [], True),
        ("sqlite", [SqliteTableOptions(without_rowid=True)], True),
        ("postgresql", [], False),
        ("postgresql", [PostgresqlTableOptions(unlogged=True)], False),
        (
            "postgresql",
            [PostgresqlTableOptions(partitioning=HashPartitioning(fields=("id",), partition_count=2))],
            True,
        ),
    ],
)
@pytest.mark.asyncio
async def test_options_and_comment_compile_one_table_change(dialect, options, rebuild):
    state = State()
    CreateModel(
        "Book",
        [("id", fields.IntField(primary_key=True, generated=False))],
        options={"table": "book", "table_description": "old comment"},
    ).state_forward("models", state)
    target = state.clone()
    operation = AlterModelOptions("Book", {"table_options": options, "table_description": "new comment"})
    operation.state_forward("models", target)
    client = FakeClient(dialect)
    editor = client.dialect.schema_editor_class(client, collect_sql=True)

    await operation.database_forward("models", state, target, editor)

    statements = editor.collected_sql
    assert sum(sql.startswith('DROP TABLE "book"') for sql in statements) == int(rebuild)
    assert sum("new comment" in sql for sql in statements) == 1
    if not rebuild and options:
        assert any("SET UNLOGGED" in sql for sql in statements)


pytestmark = pytest.mark.database_independent


@pytest.mark.asyncio
async def test_prepared_table_creation_preserves_defaults_and_matches_preview():
    class PlannedTable(Model):
        id = fields.IntField(primary_key=True)
        label = fields.TextField(db_default="prepared default")

        class Meta:
            app = "schema_plan"
            table = "planned_table"
            table_description = "prepared comment"

    apps = StateApps()
    apps.register_model("schema_plan", PlannedTable)
    apps.init_relations()
    client = FakeClient("sqlite")
    editor = client.dialect.schema_editor_class(client)
    plan = editor.table_creation.get_create_plan(PlannedTable)
    PlannedTable._meta.fields_map["label"].db_default = "later default"
    preview = client.dialect.schema_editor_class(client, collect_sql=True)
    await plan.execute(preview)
    assert client.executed == []
    await plan.execute(editor)
    assert client.executed == preview.collected_sql
    database = sqlite3.connect(":memory:")
    try:
        for statement in client.executed:
            database.executescript(statement)
        database.execute("INSERT INTO planned_table DEFAULT VALUES")
        assert database.execute("SELECT label FROM planned_table").fetchone() == ("prepared default",)
        (sql,) = database.execute("SELECT sql FROM sqlite_master WHERE name = 'planned_table'").fetchone()
        assert "prepared comment" in sql
    finally:
        database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("default_factory", [Mock, AsyncMock])
async def test_backfill_preview_does_not_evaluate_default(default_factory):
    default = default_factory(return_value="evaluated once")

    class BackfilledTable(Model):
        id = fields.IntField(primary_key=True)
        label = fields.TextField(default=default)

        class Meta:
            app = "schema_plan"
            table = "backfilled_table"

    apps = StateApps()
    apps.register_model("schema_plan", BackfilledTable)
    apps.init_relations()
    client = FakeClient("sqlite")
    editor = client.dialect.schema_editor_class(client)
    field = BackfilledTable._meta.fields_map["label"]
    plan = editor.column_backfill.get_added_column_backfill_plan(BackfilledTable, field, "label")
    assert plan.effects == frozenset({SchemaEffect.BACKFILL_ROWS})
    preview = client.dialect.schema_editor_class(client, collect_sql=True)
    await plan.execute(preview)
    default.assert_not_called()
    assert client.executed == []
    assert "runtime default" in preview.collected_sql[0]
    await plan.execute(editor)
    default.assert_called_once_with()
    if isinstance(default, AsyncMock):
        default.assert_awaited_once_with()
    assert len(client.executed) == 1
    assert "evaluated once" in client.executed[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("previous", [0, 1])
@pytest.mark.parametrize("outcome", [None, RuntimeError, asyncio.CancelledError])
async def test_legacy_rename_restores_connection_setting(monkeypatch, previous, outcome):
    database = sqlite3.connect(":memory:")
    database.execute("CREATE TABLE replacement (id INTEGER)")
    database.execute(f"PRAGMA legacy_alter_table = {previous}")
    client = FakeClient("sqlite")
    editor = client.dialect.schema_editor_class(client)

    async def execute(sql, values=None, *, recovery=None):
        cursor = database.execute(sql, values or [])
        return StatementResult(cursor.rowcount, cursor.fetchall())

    async def execute_script(sql, *, recovery=None):
        if sql.startswith("ALTER TABLE") and outcome is not None:
            raise outcome()
        database.executescript(sql)

    monkeypatch.setattr(client, "execute", execute)
    monkeypatch.setattr(client, "execute_script", execute_script)
    try:
        step = SqliteLegacyRename('ALTER TABLE "replacement" RENAME TO "original"')
        if outcome is None:
            await step.execute(editor)
            assert database.execute("SELECT name FROM sqlite_master WHERE name = 'original'").fetchone()
        else:
            with pytest.raises(outcome):
                await step.execute(editor)
        assert database.execute("PRAGMA legacy_alter_table").fetchone() == (previous,)
    finally:
        database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("release_on_success", [False, True])
async def test_schema_cleanup_finishes_under_repeated_cancellation(monkeypatch, release_on_success):
    client = FakeClient("sqlite")
    editor = client.dialect.schema_editor_class(client)
    cleanup_started = asyncio.Event()
    cleanup_release = asyncio.Event()
    cancellation_observed = asyncio.Event()
    completed = []

    async def execute_script(sql, *, recovery=None):
        if sql == "copy":
            raise ValueError("copy failed")
        cleanup_started.set()
        await cleanup_release.wait()
        completed.append(sql)

    monkeypatch.setattr(client, "execute_script", execute_script)
    plan = SchemaCleanup(
        SchemaChangePlan((SchemaStatement("copy"),)),
        SchemaChangePlan((SchemaStatement("drop"),)),
        release_on_success=release_on_success,
    )
    task = asyncio.create_task(plan.execute(editor))
    try:
        started = asyncio.create_task(cleanup_started.wait())
        try:
            done, _ = await asyncio.wait((started, task), return_when=asyncio.FIRST_COMPLETED)
            if task in done:
                await task
                pytest.fail("schema cleanup completed before reaching its release barrier")
        finally:
            started.cancel()
            await asyncio.gather(started, return_exceptions=True)
        task.cancel()
        # Scheduling the observer after cancel lets the cancelled waiter run first.
        asyncio.get_running_loop().call_soon(cancellation_observed.set)
        await cancellation_observed.wait()
        assert not task.done()
        task.cancel()
        cleanup_release.set()
        with pytest.raises(BaseExceptionGroup) as raised:
            await task
        assert isinstance(raised.value.exceptions[0], ValueError)
        assert isinstance(raised.value.exceptions[1], asyncio.CancelledError)
        assert completed == ["drop"]
    finally:
        cleanup_release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_rebuild_rejects_callable_default_before_ddl():
    default = Mock(return_value="later")

    class Before(Model):
        id = fields.IntField(primary_key=True)

        class Meta:
            app = "schema_plan"
            table = "rebuilt_table"

    class After(Model):
        id = fields.IntField(primary_key=True)
        title = fields.TextField(default=default)

        class Meta:
            app = "schema_plan"
            table = "rebuilt_table"

    apps = StateApps()
    apps.register_model("schema_plan", Before)
    apps.register_model("schema_plan", After)
    apps.init_relations()
    client = FakeClient("sqlite")
    editor = client.dialect.schema_editor_class(client)
    with pytest.raises(ConfigurationError, match="callable"):
        await editor.table_rebuild.get_rebuild_plan(Before, After).execute(editor)
    default.assert_not_called()
    assert client.executed == []


@pytest.mark.asyncio
async def test_prepared_field_addition_backfills_once_and_creates_one_index(monkeypatch):
    default = AsyncMock(return_value="prepared value")

    class Before(Model):
        id = fields.IntField(primary_key=True)

        class Meta:
            app = "schema_plan"
            table = "book"

    class After(Model):
        id = fields.IntField(primary_key=True)
        title = fields.TextField(default=default, db_index=True)

        class Meta:
            app = "schema_plan"
            table = "book"

    apps = StateApps()
    apps.register_model("schema_plan", Before)
    apps.register_model("schema_plan", After)
    apps.init_relations()
    database = sqlite3.connect(":memory:")
    client = FakeClient("sqlite")

    async def execute(sql, values=None, *, recovery=None):
        cursor = database.execute(sql, values or [])
        return StatementResult(cursor.rowcount, cursor.fetchall())

    async def execute_script(sql, *, recovery=None):
        database.executescript(sql)

    monkeypatch.setattr(client, "execute", execute)
    monkeypatch.setattr(client, "execute_script", execute_script)
    editor = client.dialect.schema_editor_class(client)
    try:
        await editor.create_model(Before)
        database.execute("INSERT INTO book DEFAULT VALUES")
        plan = editor.get_add_field_plan(Before, After, "title")
        After._meta.fields_map["title"].default = "changed declaration"
        preview = client.dialect.schema_editor_class(client, collect_sql=True)
        await plan.execute(preview)
        default.assert_not_called()
        assert SchemaEffect.BACKFILL_ROWS in plan.effects
        assert SchemaEffect.REWRITE_ROWS in plan.effects
        await plan.execute(editor)
        default.assert_awaited_once_with()
        assert database.execute("SELECT title FROM book").fetchall() == [("prepared value",)]
        assert database.execute("PRAGMA table_info(book)").fetchall()[1][3] == 1
        assert len(database.execute("PRAGMA index_list(book)").fetchall()) == 1
    finally:
        database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("dialect_name", ["sqlite", "postgresql", "clickhouse"])
async def test_offline_safety_uses_backfill_plan_without_evaluating_defaults(dialect_name, monkeypatch):
    calls = []

    async def default():
        calls.append("evaluated")
        return "value"

    state = State()
    CreateModel("Book", [("id", fields.IntField(primary_key=True))]).state_forward("models", state)
    migration = Migration(
        "0002_title", "models", operations=[AddField("Book", "title", fields.TextField(default=default))]
    )
    client = FakeClient(dialect_name)
    client.cluster = None
    client.database = "default"
    forbidden = AsyncMock(side_effect=AssertionError("offline safety performed IO"))
    monkeypatch.setattr(client, "execute", forbidden)
    monkeypatch.setattr(client, "execute_dicts", forbidden)
    monkeypatch.setattr(client, "execute_script", forbidden)
    risks = await MigrationSafetyChecker().check(
        migration, migration.prepare(state), client=client, read_catalog=False
    )
    assert [risk.code for risk in risks] == [MigrationRiskCode.ADD_FIELD_BACKFILLS_ROWS]
    assert calls == []
    forbidden.assert_not_called()


@pytest.mark.parametrize("dialect_name", ["sqlite", "postgresql", "clickhouse"])
@pytest.mark.parametrize("length", [4, 40])
def test_operation_effect_uses_prepared_rewrite_steps(dialect_name, length, monkeypatch):
    state = State()
    CreateModel(
        "Book", [("id", fields.IntField(primary_key=True)), ("title", fields.CharField(max_length=20))]
    ).state_forward("models", state)
    client = FakeClient(dialect_name)
    client.cluster = None
    client.database = "default"
    forbidden = AsyncMock(side_effect=AssertionError("effect preparation performed IO"))
    monkeypatch.setattr(client, "execute", forbidden)
    monkeypatch.setattr(client, "execute_dicts", forbidden)
    monkeypatch.setattr(client, "execute_script", forbidden)
    operation = AlterField("Book", "title", fields.CharField(max_length=length))
    effect = operation.get_effect("models", state, client.dialect.schema_editor_class(client, collect_sql=True))
    assert effect.rewrites_table is (dialect_name != "postgresql" or length == 4)
    assert state.apps.get_model("models.Book")._meta.fields_map["title"].max_length == 20
    forbidden.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["fits", "too long"])
async def test_rename_and_narrow_checks_old_column_before_ddl(value, monkeypatch):
    state = State()
    CreateModel(
        "Book", [("id", fields.IntField(primary_key=True)), ("title", fields.CharField(max_length=20))]
    ).state_forward("models", state)
    target = state.clone()
    AlterField("Book", "title", fields.CharField(max_length=4, source_field="new_title")).state_forward(
        "models", target
    )
    database = sqlite3.connect(":memory:")
    database.row_factory = sqlite3.Row
    client = FakeClient("sqlite")

    async def execute(sql, values=None, **kwargs):
        cursor = database.execute(sql, values or [])
        return StatementResult(cursor.rowcount, cursor.fetchall())

    async def execute_dicts(sql, values=None):
        return [dict(row) for row in (await execute(sql, values)).rows]

    async def execute_script(sql, *, recovery=None):
        database.executescript(sql)

    monkeypatch.setattr(client, "execute", execute)
    monkeypatch.setattr(client, "execute_dicts", execute_dicts)
    monkeypatch.setattr(client, "execute_script", execute_script)
    editor = client.dialect.schema_editor_class(client)
    try:
        await editor.create_model(state.apps.get_model("models.Book"))
        database.execute("INSERT INTO book(title) VALUES (?)", [value])
        editor.run_statement_count = 0
        plan = editor.get_alter_field_plan(
            state.apps.get_model("models.Book"), target.apps.get_model("models.Book"), "title"
        )
        if value == "too long":
            with pytest.raises(FieldNarrowingDataLossError):
                await plan.execute(editor)
            assert editor.run_statement_count == 0
            column = "title"
        else:
            await plan.execute(editor)
            column = "new_title"
        assert database.execute(f'SELECT "{column}" FROM book').fetchone()[0] == value
        assert [row[1] for row in database.execute("PRAGMA table_info(book)")] == ["id", column]
    finally:
        database.close()


@pytest.mark.asyncio
async def test_through_copy_keeps_links_and_evaluates_extra_default_once(monkeypatch):
    default = AsyncMock(return_value="copied")
    state = State()
    for operation in (
        CreateModel("Tag", [("id", fields.IntField(primary_key=True))]),
        CreateModel(
            "Note",
            [
                ("id", fields.IntField(primary_key=True)),
                ("tags", fields.ManyToManyField("models.Tag", related_name="notes")),
            ],
        ),
        CreateModel(
            "NoteTag",
            [
                ("id", fields.IntField(primary_key=True)),
                ("note", fields.ForeignKeyField("models.Note", related_name="links")),
                ("tag", fields.ForeignKeyField("models.Tag", related_name="links")),
                ("label", fields.TextField(default=default)),
            ],
        ),
    ):
        operation.state_forward("models", state)
    database = sqlite3.connect(":memory:")
    client = FakeClient("sqlite")

    async def execute_script(sql, *, recovery=None):
        database.executescript(sql)

    monkeypatch.setattr(client, "execute_script", execute_script)
    monkeypatch.setattr(client, "get_generated_key_sql", AsyncMock(return_value=None))
    editor = client.dialect.schema_editor_class(client)
    try:
        for model in state.get_models_with_tables():
            await editor.create_model(model)
        database.executescript(
            "INSERT INTO note(id) VALUES (1); INSERT INTO tag(id) VALUES (1),(2); "
            "INSERT INTO note_tag(note_id,tag_id) VALUES (1,1),(1,2);"
        )
        target = state.clone()
        AlterField(
            "Note", "tags", fields.ManyToManyField("models.Tag", related_name="notes", through="models.NoteTag")
        ).state_forward("models", target)
        plan = editor.get_alter_field_plan(
            state.apps.get_model("models.Note"),
            target.apps.get_model("models.Note"),
            "tags",
            target.get_models_with_tables(),
        )
        await plan.execute(client.dialect.schema_editor_class(client, collect_sql=True))
        default.assert_not_called()
        client.get_generated_key_sql.assert_not_called()
        await plan.execute(editor)
        default.assert_awaited_once_with()
        table = target.apps.get_model("models.NoteTag")._meta.db_table
        assert database.execute(f'SELECT note_id,tag_id,label FROM "{table}" ORDER BY tag_id').fetchall() == [
            (1, 1, "copied"),
            (1, 2, "copied"),
        ]
        assert database.execute(f'SELECT COUNT(DISTINCT id) FROM "{table}"').fetchone() == (2,)
        assert database.execute("SELECT name FROM sqlite_master WHERE name='note_tag'").fetchall() == []
    finally:
        database.close()
