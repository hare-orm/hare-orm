"""DDL is decided by the migration state alone: the SQL sqlmigrate shows for a migration is the SQL migrate
runs - dropping a unique field and a relation, renaming a constraint, rebuilding a SQLite table - and
migrate reads nothing from the database's catalog to write it."""

from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio

from hare.ddl.constraints import CheckConstraint, UniqueConstraint
from hare.dialects.postgresql.schema.postgresql_schema_editor import PostgresqlSchemaEditor
from hare.dialects.sqlite.drivers.aiosqlite.client import AiosqliteClient
from hare.fields import CharField, ForeignKeyField, IntField
from hare.migrations.migration import Migration
from hare.migrations.operations import AddConstraint, CreateModel, RemoveField, RenameConstraint
from hare.migrations.state.state import State
from hare.query.expressions import Q
from tests.utils.fake_client import FakeClient

pytestmark = pytest.mark.database_independent

APP_LABEL = "models"

MIGRATIONS = [
    Migration(
        "0001_initial",
        APP_LABEL,
        operations=[
            CreateModel(name="Author", fields=[("id", IntField(primary_key=True))]),
            CreateModel(
                name="Book",
                fields=[
                    ("id", IntField(primary_key=True)),
                    ("code", CharField(max_length=20, unique=True)),
                    ("author", ForeignKeyField(f"{APP_LABEL}.Author", related_name="books")),
                    ("rating", IntField()),
                ],
            ),
            AddConstraint("Book", UniqueConstraint(fields=("rating",), name="book_rating_unique")),
        ],
    ),
    Migration(
        "0002_changes",
        APP_LABEL,
        operations=[
            RemoveField("Book", "code"),
            RemoveField("Book", "author"),
            RenameConstraint("Book", "book_rating_unique", "book_rating_once"),
            # A CHECK lives in SQLite's CREATE TABLE - the table is rebuilt.
            AddConstraint("Book", CheckConstraint(check=Q(rating__gte=0), name="book_rating_not_negative")),
        ],
    ),
]


class RecordingClient:
    """Records every statement a client is asked to run - the DDL and any read of the catalog."""

    def __init__(self, client) -> None:
        self.client = client
        self.statements: list[str] = []
        for method_name in ("execute", "execute_dicts", "execute_script"):
            self.record(method_name)

    def record(self, method_name: str) -> None:
        method = getattr(self.client, method_name)

        async def recorded(query, *args, **kwargs):
            self.statements.append(query)
            return await method(query, *args, **kwargs)

        setattr(self.client, method_name, recorded)


@pytest_asyncio.fixture(params=["sqlite", "postgresql"])
async def client(request) -> AsyncGenerator:
    if request.param == "postgresql":
        # Nothing to run against - the statements are only recorded.
        yield FakeClient("postgresql", inline_comment=False)
        return
    sqlite_client = AiosqliteClient(file_path=":memory:", connection_alias="sqlmigrate_matches_migrate")
    await sqlite_client.create_connection(with_db=True)
    try:
        yield sqlite_client
    finally:
        await sqlite_client.close()


def get_editor(client, *, collect_sql: bool):
    if client.dialect.name == "postgresql" or isinstance(client, FakeClient):
        return PostgresqlSchemaEditor(client, collect_sql=collect_sql)
    return client.dialect.schema_editor_class(client, atomic=True, collect_sql=collect_sql)


@pytest.mark.asyncio
async def test_sqlmigrate_shows_what_migrate_runs_and_migrate_reads_no_catalog(client) -> None:
    state = State()
    for migration in MIGRATIONS:
        collecting_editor = get_editor(client, collect_sql=True)
        await migration.apply(state.clone(), schema_editor=collecting_editor, collect_sql=True)
        shown_sql = [statement for statement in collecting_editor.collected_sql if not statement.startswith("--")]

        recording_client = RecordingClient(client) if not isinstance(client, FakeClient) else None
        run_sql: list[str] = []
        editor = get_editor(client, collect_sql=False)
        original_run_sql = editor.run_sql

        async def recorded_run_sql(sql: str, original_run_sql=original_run_sql) -> None:
            run_sql.append(sql)
            await original_run_sql(sql)

        editor.run_sql = recorded_run_sql
        executed_before = len(client.executed) if isinstance(client, FakeClient) else 0
        await migration.apply(state, schema_editor=editor)

        assert run_sql == shown_sql, migration.name
        if isinstance(client, FakeClient):
            # Every statement sent to the database is DDL from the state - no catalog query.
            assert client.executed[executed_before:] == run_sql, migration.name
        else:
            assert recording_client is not None
            # The DDL runs statement by statement; a read is a SELECT or a PRAGMA asking - not setting -
            # something.
            catalog_reads = [
                statement
                for statement in recording_client.statements
                if statement.lstrip().upper().startswith("SELECT")
                or (statement.lstrip().upper().startswith("PRAGMA") and "=" not in statement)
            ]
            assert catalog_reads == [], migration.name
