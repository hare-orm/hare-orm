"""The migration safety check: every rule on PostgreSQL's and SQLite's dialects without a database
(every existing table may be large), exemptions, nested SeparateDatabaseAndState operations, and
row counting on the database under test."""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from hare import Connections
from hare.ddl.constraints import CheckConstraint, UniqueConstraint
from hare.ddl.indexes.index import Index
from hare.dialects.postgresql.constants import POSTGRESQL_DIALECT
from hare.dialects.postgresql.migration_safety.volatile_default_rule import VolatileDefaultRule
from hare.dialects.sqlite.constants import SQLITE_DIALECT
from hare.fields import (
    BigIntField,
    CharField,
    DatetimeField,
    ForeignKeyField,
    IntField,
    ManyToManyField,
    Now,
    RandomHex,
    SqlDefault,
    UUIDField,
    UuidV7,
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
from hare.query.expressions import Q
from tests.utils.fake_client import FakeClient

BOOK_TABLE = "safety_book"
AUTHOR_TABLE = "safety_author"


@pytest.mark.database_independent
@pytest.mark.parametrize(
    ("field", "message"),
    [
        (DatetimeField(db_default=Now()), None),
        (CharField(max_length=32, db_default=RandomHex()), "is volatile"),
        (UUIDField(db_default=UuidV7()), "is volatile"),
        (DatetimeField(db_default=SqlDefault("now()")), "unknown volatility"),
        (IntField(db_default=SqlDefault("42")), "unknown volatility"),
    ],
)
def test_default_volatility_uses_declarations_without_rendering_sql(monkeypatch, field, message):
    render = Mock(side_effect=AssertionError("safety classification must not render SQL"))
    monkeypatch.setattr(field.db_default, "get_sql", render)
    context = SimpleNamespace(
        operation=AddField("Book", "added", field),
        may_be_large=lambda model: True,
        get_field_after=lambda model, name: field,
        get_risk=lambda code, message, alternative: SimpleNamespace(code=code, message=message),
    )
    risk = VolatileDefaultRule().check_operation(context)
    if message is None:
        assert risk is None
    else:
        assert risk.code == MigrationRiskCode.ADD_FIELD_VOLATILE_DEFAULT
        assert message in risk.message
    render.assert_not_called()


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
    state = State()
    for operation in get_initial_operations():
        operation.state_forward("models", state)
    return state


def make_migration(operations: list[Operation], *, atomic: bool = True, exemptions=()) -> Migration:
    migration = Migration("0002_change", "models", operations=operations)
    migration.atomic = atomic
    migration.safety_exemptions = list(exemptions)
    return migration


async def check(dialect, operations: list[Operation], **migration_options) -> list[MigrationRisk]:
    client = FakeClient(dialect.name)
    # Safety rules inspect the dialect capabilities, not the recording fake's non-transactional execution mode.
    client.features = dialect.features
    migration = make_migration(operations, **migration_options)
    return await MigrationSafetyChecker().check(
        migration,
        migration.prepare(get_initial_state()),
        client=client,
        read_catalog=False,
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
    (
        "NOT NULL field with no default",
        lambda: [AddField("Book", "isbn", CharField(max_length=13))],
        {},
        [R.ADD_FIELD_WITHOUT_DEFAULT],
        [R.ADD_FIELD_WITHOUT_DEFAULT],
    ),
    (
        "NOT NULL foreign key with no default",
        lambda: [AddField("Book", "editor", ForeignKeyField("models.Author", db_index=False, db_constraint=False))],
        {},
        [R.ADD_FIELD_WITHOUT_DEFAULT],
        [R.ADD_FIELD_WITHOUT_DEFAULT],
    ),
    (
        "nullable field with no default",
        lambda: [AddField("Book", "isbn", CharField(max_length=13, null=True))],
        {},
        [],
        [],
    ),
    ("many-to-many relation", lambda: [AddField("Book", "authors", ManyToManyField("models.Author"))], {}, [], []),
    ("constant database default", lambda: [AddField("Book", "rating", IntField(db_default=0))], {}, [], []),
    (
        "volatile database default",
        lambda: [AddField("Book", "code", UUIDField(db_default=SqlDefault("gen_random_uuid()")))],
        {},
        [R.ADD_FIELD_VOLATILE_DEFAULT],
        [R.ADD_FIELD_BACKFILLS_ROWS],
    ),
    (
        "stable database default",
        lambda: [AddField("Book", "added", DatetimeField(db_default=Now()))],
        {},
        [],
        [R.ADD_FIELD_BACKFILLS_ROWS],
    ),
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


@pytest.mark.database_independent
@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["renamed", "created", "recreated", "state_only", "populated"])
async def test_nested_safety_uses_each_physical_table_before_and_after_changes(monkeypatch, scenario):
    client = FakeClient("postgresql")
    count = AsyncMock(return_value=10)
    monkeypatch.setattr(client.dialect.migration_safety_rules, "count_table_rows", count)
    state = get_initial_state()
    fields = [("id", IntField(primary_key=True)), ("title", CharField(max_length=50))]
    if scenario == "renamed":
        operations = [
            RenameModel("Book", "Volume"),
            AlterModelTable("Volume", "volumes"),
            AddIndex("Volume", Index(fields=("title",), name="volume_title")),
        ]
    elif scenario == "recreated":
        operations = [
            AddIndex("Book", Index(fields=("title",), name="old_title")),
            DeleteModel("Book"),
            CreateModel("Book", fields=fields, options={"table": BOOK_TABLE}),
            AddIndex("Book", Index(fields=("title",), name="new_title")),
        ]
    else:
        operations = [CreateModel("Note", fields=fields, state_only=scenario == "state_only")]
        if scenario == "populated":
            operations.append(RunSQL("INSERT INTO note(id, title) VALUES (1, 'a')"))
        operations.extend(
            [
                RenameModel("Note", "Volume"),
                AddIndex("Volume", Index(fields=("title",), name="volume_title")),
            ]
        )
    migration = make_migration([SeparateDatabaseAndState(database_operations=operations)])
    risks = await MigrationSafetyChecker(large_table_rows=10).check(migration, migration.prepare(state), client=client)
    index_risks = [risk for risk in risks if risk.code is R.ADD_INDEX_WITHOUT_CONCURRENTLY]
    assert len(index_risks) == (0 if scenario == "created" else 1)
    if scenario in {"renamed", "recreated"}:
        count.assert_awaited_once_with(client, BOOK_TABLE, None, 10)
    else:
        count.assert_not_awaited()
    assert set(state.models) == {("models", "Author"), ("models", "Book")}


@pytest.mark.database_independent
@pytest.mark.asyncio
@pytest.mark.parametrize("created", [False, True])
@pytest.mark.parametrize("nested_code", [False, True])
async def test_migration_wide_lock_rules_cross_database_container_boundaries(created, nested_code):
    schema_change = (
        CreateModel("Note", fields=[("id", IntField(primary_key=True))])
        if created
        else AddField("Book", "rating", IntField(null=True))
    )
    code = RunPython(run_nothing)
    operations = (
        [schema_change, SeparateDatabaseAndState(database_operations=[code])]
        if nested_code
        else [SeparateDatabaseAndState(database_operations=[schema_change]), code]
    )
    risks = await check(POSTGRESQL_DIALECT, operations)
    assert [risk.code for risk in risks] == ([] if created else [R.SCHEMA_CHANGE_WITH_RUN_PYTHON])


@pytest.mark.database_independent
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


@pytest.mark.database_independent
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
    state = State()
    initial = Migration("0001_initial", "models", operations=get_initial_operations())
    try:
        state = await initial.apply(state, schema_editor=editor)
        await connection.execute_script(f"INSERT INTO {BOOK_TABLE} (id, title) VALUES (1, 'a'), (2, 'b'), (3, 'c')")
        operations = [RenameField("Book", "title", "name"), AddField("Book", "rating", IntField(default=0))]
        for large_table_rows, expected_codes in [
            (3, [MigrationRiskCode.RENAME_FIELD, MigrationRiskCode.ADD_FIELD_BACKFILLS_ROWS]),
            (4, [MigrationRiskCode.RENAME_FIELD]),
        ]:
            migration = make_migration(operations)
            risks = await MigrationSafetyChecker(large_table_rows=large_table_rows).check(
                migration, migration.prepare(state), client=connection
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
async def test_a_column_with_no_default_is_a_risk_only_on_a_table_holding_rows(db_simple):
    """makemigrations wrote ADD COLUMN ... NOT NULL without a default and reported nothing - migrate failed on
    any table holding a row."""
    connection = Connections.get("models")
    editor = connection.dialect.schema_editor_class(connection, atomic=True, collect_sql=False)
    state = State()
    initial = Migration("0001_initial", "models", operations=get_initial_operations())
    operations = [AddField("Book", "isbn", CharField(max_length=13))]
    try:
        state = await initial.apply(state, schema_editor=editor)
        for insert_sql, expected_codes in [
            (None, []),
            (f"INSERT INTO {BOOK_TABLE} (id, title) VALUES (1, 'a')", [MigrationRiskCode.ADD_FIELD_WITHOUT_DEFAULT]),
        ]:
            if insert_sql is not None:
                await connection.execute_script(insert_sql)
            migration = make_migration(operations)
            risks = await MigrationSafetyChecker().check(migration, migration.prepare(state), client=connection)
            assert [risk.code for risk in risks] == expected_codes
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
    state = State()
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
    state = State()
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
    state = State()
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


@pytest.mark.database_independent
@pytest.mark.asyncio
async def test_safety_reads_prepared_states_without_preparing_again(monkeypatch):
    migration = make_migration([AddField("Book", "isbn", CharField(max_length=13))])
    state = get_initial_state()
    prepared = migration.prepare(state)
    prepare = Mock(side_effect=AssertionError("operation states are already prepared"))
    monkeypatch.setattr(migration, "prepare", prepare)
    client = FakeClient(SQLITE_DIALECT.name)
    client.features = SQLITE_DIALECT.features

    risks = await MigrationSafetyChecker().check(migration, prepared, client=client, read_catalog=False)

    assert MigrationRiskCode.ADD_FIELD_WITHOUT_DEFAULT in {risk.code for risk in risks}
    assert "isbn" not in state.models[("models", "Book")].fields
    assert "isbn" in prepared.after.models[("models", "Book")].fields
    prepare.assert_not_called()
