"""The migration safety check: every rule on PostgreSQL's and SQLite's dialects without a database
(every existing table may be large), exemptions, nested SeparateDatabaseAndState operations, and
row counting on the database under test."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from hare import Connections
from hare.ddl.constraints import CheckConstraint, UniqueConstraint
from hare.ddl.indexes.index import Index
from hare.dialects.postgresql.constants import POSTGRESQL_DIALECT
from hare.dialects.sqlite.constants import SQLITE_DIALECT
from hare.exceptions import ConfigurationError
from hare.fields import (
    BigIntField,
    CharField,
    DatetimeField,
    ForeignKeyField,
    IntField,
    Now,
    SqlDefault,
    UUIDField,
)
from hare.migrations.migration import Migration
from hare.migrations.operations import (
    AddConstraint,
    AddField,
    AddIndex,
    AlterColumnNotNullSafe,
    AlterField,
    AlterModelTable,
    CreateModel,
    DeleteModel,
    Operation,
    RemoveField,
    RenameField,
    RenameModel,
    RunPython,
    RunSQL,
    SeparateDatabaseAndState,
)
from hare.migrations.safety import MigrationRisk, MigrationRiskCode, MigrationSafetyChecker
from hare.migrations.state.state import State
from hare.migrations.state.state_apps import StateApps
from hare.query.expressions import Q

BOOK_TABLE = "safety_book"
AUTHOR_TABLE = "safety_author"


def get_initial_operations() -> list[Operation]:
    return [
        CreateModel(
            name="Author",
            fields=[("id", IntField(primary_key=True)), ("name", CharField(max_length=50))],
            options={"table": AUTHOR_TABLE},
        ),
        CreateModel(
            name="Book",
            fields=[
                ("id", IntField(primary_key=True)),
                ("title", CharField(max_length=50, null=True)),
                ("pages", IntField(null=True)),
            ],
            options={"table": BOOK_TABLE},
        ),
    ]


def get_initial_state() -> State:
    state = State(models={}, apps=StateApps())
    for operation in get_initial_operations():
        operation.state_forward("models", state)
    return state


def make_migration(operations: list[Operation], *, atomic: bool = True, exemptions=()) -> Migration:
    migration = Migration("0002_change", "models", operations=operations)
    migration.atomic = atomic
    migration.safety_exemptions = list(exemptions)
    return migration


async def check(dialect, operations: list[Operation], **migration_options) -> list[MigrationRisk]:
    return await MigrationSafetyChecker().check(
        make_migration(operations, **migration_options), get_initial_state(), dialect=dialect
    )


def run_nothing(apps, schema_editor) -> None:
    return None


R = MigrationRiskCode
OPERATION_CASES: list[tuple[str, Callable[[], list[Operation]], dict, list, list]] = [
    (
        "index",
        lambda: [AddIndex("Book", Index(fields=("title",), name="book_title"))],
        {},
        [R.ADD_INDEX_WITHOUT_CONCURRENTLY],
        [],
    ),
    (
        "index built concurrently",
        lambda: [AddIndex("Book", Index(fields=("title",), name="book_title"), concurrently=True)],
        {"atomic": False},
        [],
        [],
    ),
    (
        "NOT NULL field with a Python default",
        lambda: [AddField("Book", "rating", IntField(default=0))],
        {},
        [R.ADD_FIELD_BACKFILLS_ROWS],
        [R.ADD_FIELD_BACKFILLS_ROWS],
    ),
    ("constant database default", lambda: [AddField("Book", "rating", IntField(db_default=0))], {}, [], []),
    (
        "volatile database default",
        lambda: [AddField("Book", "code", UUIDField(db_default=SqlDefault("gen_random_uuid()")))],
        {},
        [R.ADD_FIELD_VOLATILE_DEFAULT],
        [],
    ),
    ("stable database default", lambda: [AddField("Book", "added", DatetimeField(db_default=Now()))], {}, [], []),
    (
        "integer widened to bigint",
        lambda: [AlterField("Book", "pages", BigIntField(null=True))],
        {},
        [R.ALTER_FIELD_REWRITES_TABLE],
        [R.ALTER_FIELD_REWRITES_TABLE],
    ),
    (
        "varchar made longer",
        lambda: [AlterField("Book", "title", CharField(max_length=100, null=True))],
        {},
        [],
        [R.ALTER_FIELD_REWRITES_TABLE],
    ),
    (
        "NOT NULL set by AlterField",
        lambda: [AlterField("Book", "title", CharField(max_length=50))],
        {},
        [R.SET_NOT_NULL_SCANS_TABLE],
        [R.ALTER_FIELD_REWRITES_TABLE],
    ),
    (
        "AlterColumnNotNullSafe in a transaction",
        lambda: [AlterColumnNotNullSafe("Book", "title")],
        {},
        [R.SET_NOT_NULL_SCANS_TABLE],
        [],
    ),
    (
        "AlterColumnNotNullSafe without a transaction",
        lambda: [AlterColumnNotNullSafe("Book", "title")],
        {"atomic": False},
        [],
        [],
    ),
    (
        "check constraint",
        lambda: [AddConstraint("Book", CheckConstraint(check=Q(pages__gte=0), name="pages_positive"))],
        {},
        [R.ADD_CHECK_CONSTRAINT_VALIDATES_ROWS],
        [],
    ),
    (
        "check constraint not validated",
        lambda: [AddConstraint("Book", CheckConstraint(check=Q(pages__gte=0), name="pages_positive"), not_valid=True)],
        {},
        [],
        [],
    ),
    (
        "unique constraint",
        lambda: [AddConstraint("Book", UniqueConstraint(fields=("title",), name="book_title_unique"))],
        {},
        [R.ADD_UNIQUE_CONSTRAINT_BUILDS_INDEX],
        [],
    ),
    (
        "unique constraint over an index built concurrently",
        lambda: [
            AddIndex("Book", Index(fields=("title",), name="book_title_unique", unique=True), concurrently=True),
            AddConstraint(
                "Book", UniqueConstraint(fields=("title",), name="book_title_unique"), using_index="book_title_unique"
            ),
        ],
        {"atomic": False},
        [],
        [],
    ),
    (
        "foreign key",
        lambda: [AddField("Book", "editor", ForeignKeyField("models.Author", null=True, db_index=False))],
        {},
        [R.ADD_FOREIGN_KEY_VALIDATES_ROWS],
        [],
    ),
    (
        "indexed foreign key",
        lambda: [AddField("Book", "editor", ForeignKeyField("models.Author", null=True))],
        {},
        [R.ADD_INDEX_WITHOUT_CONCURRENTLY, R.ADD_FOREIGN_KEY_VALIDATES_ROWS],
        [],
    ),
    (
        "foreign key not validated",
        lambda: [
            AddField("Book", "editor", ForeignKeyField("models.Author", null=True, db_index=False), not_valid=True)
        ],
        {},
        [],
        [],
    ),
    (
        "foreign key without a constraint",
        lambda: [
            AddField(
                "Book", "editor", ForeignKeyField("models.Author", null=True, db_index=False, db_constraint=False)
            )
        ],
        {},
        [],
        [],
    ),
    (
        "field added with an index",
        lambda: [AddField("Book", "isbn", CharField(max_length=13, null=True, db_index=True))],
        {},
        [R.ADD_INDEX_WITHOUT_CONCURRENTLY],
        [],
    ),
    (
        "field altered to get an index",
        lambda: [AlterField("Book", "pages", IntField(null=True, db_index=True))],
        {},
        [R.ADD_INDEX_WITHOUT_CONCURRENTLY],
        [],
    ),
    (
        "unique field",
        lambda: [AddField("Book", "isbn", CharField(max_length=13, null=True, unique=True))],
        {},
        [R.ADD_UNIQUE_CONSTRAINT_BUILDS_INDEX],
        [],
    ),
    ("renamed field", lambda: [RenameField("Book", "title", "name")], {}, [R.RENAME_FIELD], [R.RENAME_FIELD]),
    ("removed field", lambda: [RemoveField("Book", "pages")], {}, [R.REMOVE_FIELD], [R.REMOVE_FIELD]),
    (
        "field removed from the models only",
        lambda: [SeparateDatabaseAndState(state_operations=[RemoveField("Book", "pages")])],
        {},
        [],
        [],
    ),
    (
        "column dropped apart from the models",
        lambda: [SeparateDatabaseAndState(database_operations=[RemoveField("Book", "pages")])],
        {},
        [],
        [],
    ),
    (
        "index built apart from the models",
        lambda: [SeparateDatabaseAndState(database_operations=[AddIndex("Book", Index(fields=("title",), name="t"))])],
        {},
        [R.ADD_INDEX_WITHOUT_CONCURRENTLY],
        [],
    ),
    (
        "renamed model",
        lambda: [
            CreateModel(name="Reader", fields=[("id", IntField(primary_key=True))], state_only=True),
            RenameModel("Reader", "Subscriber"),
        ],
        {},
        [R.RENAME_MODEL],
        [R.RENAME_MODEL],
    ),
    ("renamed model keeping its table", lambda: [RenameModel("Book", "Volume")], {}, [], []),
    ("renamed table", lambda: [AlterModelTable("Book", "volumes")], {}, [R.RENAME_MODEL], [R.RENAME_MODEL]),
    ("deleted model", lambda: [DeleteModel("Book")], {}, [R.DELETE_MODEL], [R.DELETE_MODEL]),
    ("model deleted from the models only", lambda: [DeleteModel("Book", state_only=True)], {}, [], []),
    ("raw SQL", lambda: [RunSQL("SELECT 1")], {}, [R.RUN_SQL], [R.RUN_SQL]),
    (
        "Python code beside a schema change",
        lambda: [RunPython(run_nothing), AddField("Book", "rating", IntField(null=True))],
        {},
        [R.SCHEMA_CHANGE_WITH_RUN_PYTHON],
        [R.SCHEMA_CHANGE_WITH_RUN_PYTHON],
    ),
    (
        "Python code beside a schema change without a transaction",
        lambda: [RunPython(run_nothing), AddField("Book", "rating", IntField(null=True))],
        {"atomic": False},
        [],
        [],
    ),
    (
        "table created by the same migration",
        lambda: [
            CreateModel(name="Note", fields=[("id", IntField(primary_key=True)), ("text", CharField(max_length=9))]),
            AddIndex("Note", Index(fields=("text",), name="note_text")),
            AddField("Note", "rating", IntField(default=0)),
            AddConstraint("Note", UniqueConstraint(fields=("text",), name="note_text_unique")),
            RunPython(run_nothing),
        ],
        {},
        [],
        [],
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("make_operations", "migration_options", "postgresql_codes", "sqlite_codes"),
    [pytest.param(*case[1:], id=case[0]) for case in OPERATION_CASES],
)
async def test_each_rule_on_each_dialect(make_operations, migration_options, postgresql_codes, sqlite_codes):
    postgresql_risks = await check(POSTGRESQL_DIALECT, make_operations(), **migration_options)
    sqlite_risks = await check(SQLITE_DIALECT, make_operations(), **migration_options)
    assert [risk.code for risk in postgresql_risks] == postgresql_codes
    assert [risk.code for risk in sqlite_risks] == sqlite_codes
    for risk in [*postgresql_risks, *sqlite_risks]:
        assert risk.app_label == "models"
        assert risk.migration_name == "0002_change"
        assert risk.message
        assert risk.safe_alternative
        assert not risk.exempted


@pytest.mark.asyncio
async def test_a_risk_the_migration_exempts_is_marked():
    risks = await check(
        POSTGRESQL_DIALECT,
        [RunSQL("SELECT 1"), AddIndex("Book", Index(fields=("title",), name="book_title"))],
        exemptions=[MigrationRiskCode.RUN_SQL],
    )
    assert [(risk.code, risk.exempted) for risk in risks] == [
        (MigrationRiskCode.RUN_SQL, True),
        (MigrationRiskCode.ADD_INDEX_WITHOUT_CONCURRENTLY, False),
    ]
    assert str(risks[0]).startswith("models.0002_change: Run SQL [run_sql] Raw SQL")


@pytest.mark.asyncio
async def test_an_unknown_exemption_is_refused():
    with pytest.raises(ConfigurationError, match="safety_exemptions takes MigrationRiskCode members"):
        await check(POSTGRESQL_DIALECT, [RunSQL("SELECT 1")], exemptions=["run_sqll"])


@pytest.mark.parametrize("large_table_rows", [-1, 10**12 + 1, 1.5, True, "100"])
def test_large_table_rows_is_checked(large_table_rows):
    with pytest.raises(ConfigurationError, match="large_table_rows"):
        MigrationSafetyChecker(large_table_rows=large_table_rows)


@pytest.mark.parametrize(
    ("old_type", "new_type", "rewrites"),
    [
        ("VARCHAR(50)", "VARCHAR(100)", False),
        ("VARCHAR(100)", "VARCHAR(50)", True),
        ("VARCHAR(50)", "TEXT", False),
        ("TEXT", "VARCHAR(50)", True),
        ("TEXT", "VARCHAR", False),
        ("DECIMAL(10,2)", "DECIMAL(12,2)", False),
        ("DECIMAL(10,2)", "DECIMAL(12,3)", True),
        ("DECIMAL(10,2)", "DECIMAL(9,2)", True),
        ("DECIMAL(10,2)", "NUMERIC", False),
        ("NUMERIC", "DECIMAL(10,2)", True),
        ("INT", "BIGINT", True),
        ("CIDR", "INET", False),
        ("TIMESTAMP", "TIMESTAMPTZ", True),
        ("UUID", "uuid", False),
    ],
)
def test_postgresql_tells_which_type_changes_rewrite_the_table(old_type, new_type, rewrites):
    editor_class = POSTGRESQL_DIALECT.schema_editor_class
    assert editor_class.column_type_changes_class.column_type_change_rewrites_table(old_type, new_type) is rewrites


@pytest.mark.asyncio
async def test_the_database_tells_a_small_table_from_a_large_one(db_simple):
    connection = Connections.get("models")
    editor = connection.dialect.schema_editor_class(connection, atomic=True, collect_sql=False)
    state = State(models={}, apps=StateApps())
    initial = Migration("0001_initial", "models", operations=get_initial_operations())
    try:
        state = await initial.apply(state, schema_editor=editor)
        await connection.execute_script(f"INSERT INTO {BOOK_TABLE} (id, title) VALUES (1, 'a'), (2, 'b'), (3, 'c')")
        operations = [RenameField("Book", "title", "name"), AddField("Book", "rating", IntField(default=0))]
        for large_table_rows, expected_codes in [
            (3, [MigrationRiskCode.RENAME_FIELD, MigrationRiskCode.ADD_FIELD_BACKFILLS_ROWS]),
            (4, [MigrationRiskCode.RENAME_FIELD]),
        ]:
            risks = await MigrationSafetyChecker(large_table_rows=large_table_rows).check(
                make_migration(operations), state, dialect=connection.dialect, client=connection
            )
            assert [risk.code for risk in risks] == expected_codes
        # A model the database has no table for yet holds no rows.
        assert (
            await connection.dialect.migration_safety_rules.count_table_rows(connection, "safety_missing", None, 10)
            == 0
        )
    finally:
        for table in (BOOK_TABLE, AUTHOR_TABLE):
            await connection.execute_script(
                f"DROP TABLE IF EXISTS {connection.dialect.literals.quote_identifier(table)}"
            )


@pytest.mark.asyncio
async def test_an_alter_field_of_the_python_side_only_keeps_the_table(db_simple):
    """A change the column's declaration doesn't show (sensitive=) neither rewrites the table nor
    is reported as rewriting it - on SQLite, which rebuilds a table for any other change."""
    python_side_only = [AlterField("Book", "title", CharField(max_length=50, null=True, sensitive=True))]
    declaration_change = [AlterField("Book", "title", CharField(max_length=50))]
    for dialect in (POSTGRESQL_DIALECT, SQLITE_DIALECT):
        assert await check(dialect, python_side_only) == []
    assert [risk.code for risk in await check(SQLITE_DIALECT, declaration_change)] == [R.ALTER_FIELD_REWRITES_TABLE]

    connection = Connections.get("models")
    state = State(models={}, apps=StateApps())
    editor = connection.dialect.schema_editor_class(connection, atomic=True, collect_sql=False)
    try:
        state = await Migration("0001_initial", "models", operations=get_initial_operations()).apply(
            state, schema_editor=editor
        )
        collecting_editor = connection.dialect.schema_editor_class(connection, atomic=True, collect_sql=True)
        await make_migration(python_side_only).apply(state, schema_editor=collecting_editor)
        assert not any("CREATE TABLE" in sql or "ALTER" in sql for sql in collecting_editor.collected_sql)
    finally:
        for table in (BOOK_TABLE, AUTHOR_TABLE):
            await connection.execute_script(
                f"DROP TABLE IF EXISTS {connection.dialect.literals.quote_identifier(table)}"
            )


@pytest.mark.asyncio
async def test_an_alter_field_renaming_the_column_only_renames_it(db_simple):
    """A new source_field alone is a RENAME COLUMN - the field copied into the migration state
    has the same "no database default" as before."""
    renaming = [AlterField("Book", "title", CharField(max_length=50, null=True, source_field="headline"))]
    assert await check(SQLITE_DIALECT, renaming) == []
    connection = Connections.get("models")
    state = State(models={}, apps=StateApps())
    editor = connection.dialect.schema_editor_class(connection, atomic=True, collect_sql=False)
    try:
        state = await Migration("0001_initial", "models", operations=get_initial_operations()).apply(
            state, schema_editor=editor
        )
        collecting_editor = connection.dialect.schema_editor_class(connection, atomic=True, collect_sql=True)
        await make_migration(renaming).apply(state, schema_editor=collecting_editor)
        statements = "\n".join(collecting_editor.collected_sql)
        assert "RENAME COLUMN" in statements
        assert "CREATE TABLE" not in statements
    finally:
        for table in (BOOK_TABLE, AUTHOR_TABLE):
            await connection.execute_script(
                f"DROP TABLE IF EXISTS {connection.dialect.literals.quote_identifier(table)}"
            )


@pytest.mark.asyncio
async def test_an_alter_field_of_the_index_only_creates_or_drops_the_index(db_simple):
    """db_index= alone is a CREATE INDEX or DROP INDEX - SQLite's table isn't rebuilt for it."""
    indexing = [AlterField("Book", "title", CharField(max_length=50, null=True, db_index=True))]
    assert R.ALTER_FIELD_REWRITES_TABLE not in [risk.code for risk in await check(SQLITE_DIALECT, indexing)]
    connection = Connections.get("models")
    state = State(models={}, apps=StateApps())
    editor = connection.dialect.schema_editor_class(connection, atomic=True, collect_sql=False)
    try:
        state = await Migration("0001_initial", "models", operations=get_initial_operations()).apply(
            state, schema_editor=editor
        )
        collecting_editor = connection.dialect.schema_editor_class(connection, atomic=True, collect_sql=True)
        await make_migration(indexing).apply(state.clone(), schema_editor=collecting_editor)
        statements = "\n".join(collecting_editor.collected_sql)
        assert "CREATE INDEX" in statements
        assert "CREATE TABLE" not in statements
        state = await make_migration(indexing).apply(state, schema_editor=editor)
        unindexing = [AlterField("Book", "title", CharField(max_length=50, null=True))]
        collecting_editor = connection.dialect.schema_editor_class(connection, atomic=True, collect_sql=True)
        await make_migration(unindexing).apply(state.clone(), schema_editor=collecting_editor)
        statements = "\n".join(collecting_editor.collected_sql)
        assert "DROP INDEX" in statements
        assert "CREATE TABLE" not in statements
    finally:
        for table in (BOOK_TABLE, AUTHOR_TABLE):
            await connection.execute_script(
                f"DROP TABLE IF EXISTS {connection.dialect.literals.quote_identifier(table)}"
            )
