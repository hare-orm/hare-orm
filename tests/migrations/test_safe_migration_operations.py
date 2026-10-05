"""The operations the migration safety check recommends: a foreign key added unvalidated, a unique
constraint taking over an index built concurrently, NOT NULL set through a validated CHECK - applied,
unapplied, written to a migration file and read back - and the ``migrations.safety`` setting."""

from __future__ import annotations

import pytest

from hare import Connections
from hare.contrib.test import requires_features
from hare.core.config import HareConfig, MigrationSafetyConfig, MigrationsConfig
from hare.ddl.constraints import UniqueConstraint
from hare.ddl.generated_names import GeneratedNames
from hare.ddl.indexes.index import Index
from hare.exceptions import ConfigurationError, IntegrityError
from hare.fields import CharField, ForeignKeyField, IntField
from hare.migrations.migration import Migration
from hare.migrations.operations import (
    AddConstraint,
    AddField,
    AddIndex,
    AlterColumnNotNullSafe,
    CreateModel,
    ValidateConstraint,
)
from hare.migrations.state.state import State
from hare.migrations.state.state_apps import StateApps
from hare.migrations.writer import MigrationWriter

AUTHOR_TABLE = "safe_author"
BOOK_TABLE = "safe_book"


def make_migration(name: str, *operations, atomic: bool = True) -> Migration:
    migration = Migration(name=name, app_label="models", operations=list(operations))
    migration.atomic = atomic
    return migration


def get_initial_migration() -> Migration:
    return make_migration(
        "0001_initial",
        CreateModel(
            name="Author",
            fields=[("id", IntField(primary_key=True)), ("name", CharField(max_length=20))],
            options={"table": AUTHOR_TABLE},
        ),
        CreateModel(
            name="Book",
            fields=[("id", IntField(primary_key=True)), ("title", CharField(max_length=20, null=True))],
            options={"table": BOOK_TABLE},
        ),
    )


class SafeTables:
    """The two tables of the initial migration on the database under test, dropped afterwards."""

    def __init__(self) -> None:
        self.connection = Connections.get("models")

    def get_editor(self, *, atomic: bool):
        return self.connection.dialect.schema_editor_class(self.connection, atomic=atomic, collect_sql=False)

    def quote(self, name: str) -> str:
        return self.connection.dialect.literals.quote_identifier(name)

    async def create(self) -> State:
        state = State(models={}, apps=StateApps())
        state = await get_initial_migration().apply(state, schema_editor=self.get_editor(atomic=True))
        await self.connection.execute_script(f"INSERT INTO {self.quote(AUTHOR_TABLE)} (id, name) VALUES (1, 'a')")
        await self.connection.execute_script(
            f"INSERT INTO {self.quote(BOOK_TABLE)} (id, title) VALUES (1, 'x'), (2, 'y')"
        )
        return state

    async def drop(self) -> None:
        for table in (BOOK_TABLE, AUTHOR_TABLE):
            await self.connection.execute_script(f"DROP TABLE IF EXISTS {self.quote(table)}")


@pytest.mark.asyncio
@requires_features(supports_foreign_keys=True)
async def test_a_foreign_key_added_unvalidated_checks_new_rows_and_validates_later(db_simple):
    tables = SafeTables()
    try:
        state = await tables.create()
        before = state.clone()
        foreign_key_name = GeneratedNames.get_foreign_key_name(BOOK_TABLE, ("editor_id",), AUTHOR_TABLE, ("id",))
        migration = make_migration(
            "0002_editor",
            AddField("Book", "editor", ForeignKeyField("models.Author", null=True, db_index=False), not_valid=True),
            ValidateConstraint("Book", foreign_key_name),
        )
        state = await migration.apply(state, schema_editor=tables.get_editor(atomic=True))
        await tables.connection.execute_script(f"UPDATE {tables.quote(BOOK_TABLE)} SET editor_id = 1 WHERE id = 1")
        with pytest.raises(IntegrityError):
            await tables.connection.execute_script(
                f"UPDATE {tables.quote(BOOK_TABLE)} SET editor_id = 99 WHERE id = 2"
            )
        await migration.unapply(before, schema_editor=tables.get_editor(atomic=True))
        rows = await tables.connection.execute_dicts(f"SELECT * FROM {tables.quote(BOOK_TABLE)} ORDER BY id")
        assert [sorted(row) for row in rows] == [["id", "title"], ["id", "title"]]
    finally:
        await tables.drop()


def test_not_valid_takes_only_a_foreign_key_with_a_constraint():
    with pytest.raises(ConfigurationError, match="not_valid=True takes a ForeignKeyField"):
        AddField("Book", "pages", IntField(null=True), not_valid=True)
    with pytest.raises(ConfigurationError, match="not_valid=True takes a ForeignKeyField"):
        AddField("Book", "editor", ForeignKeyField("models.Author", null=True, db_constraint=False), not_valid=True)


@pytest.mark.asyncio
@requires_features(supports_unique_constraints=True)
async def test_a_unique_constraint_takes_over_an_index_built_concurrently(db_simple):
    tables = SafeTables()
    try:
        state = await tables.create()
        before = state.clone()
        migration = make_migration(
            "0002_unique_title",
            AddIndex("Book", Index(fields=("title",), name="safe_book_title", unique=True), concurrently=True),
            AddConstraint(
                "Book", UniqueConstraint(fields=("title",), name="safe_book_title"), using_index="safe_book_title"
            ),
            atomic=False,
        )
        state = await migration.apply(state, schema_editor=tables.get_editor(atomic=False))
        book_state = state.models[("models", "Book")]
        assert [index.name for index in book_state.get_option_list("indexes")] == []
        assert [constraint.name for constraint in book_state.get_option_list("constraints")] == ["safe_book_title"]
        with pytest.raises(IntegrityError):
            await tables.connection.execute_script(
                f"INSERT INTO {tables.quote(BOOK_TABLE)} (id, title) VALUES (3, 'x')"
            )
        # Going back, the index is the index of AddIndex again, then dropped by it.
        partly_back = before.clone()
        make_migration("0002_index_only", migration.operations[0]).operations[0].state_forward("models", partly_back)
        await make_migration("0002_constraint_only", migration.operations[1], atomic=False).unapply(
            partly_back, schema_editor=tables.get_editor(atomic=False)
        )
        with pytest.raises(IntegrityError):
            await tables.connection.execute_script(
                f"INSERT INTO {tables.quote(BOOK_TABLE)} (id, title) VALUES (3, 'x')"
            )
        await make_migration("0002_index_only", migration.operations[0], atomic=False).unapply(
            before, schema_editor=tables.get_editor(atomic=False)
        )
        await tables.connection.execute_script(f"INSERT INTO {tables.quote(BOOK_TABLE)} (id, title) VALUES (3, 'x')")
    finally:
        await tables.drop()


def test_using_index_takes_only_a_named_unique_constraint_without_a_condition():
    from hare.ddl.constraints import CheckConstraint
    from hare.query.expressions import Q

    for constraint in (
        UniqueConstraint(fields=("title",)),
        UniqueConstraint(fields=("title",), name="u", condition=Q(title__isnull=False)),
        CheckConstraint(check=Q(title__isnull=False), name="c"),
    ):
        with pytest.raises(ConfigurationError, match="using_index takes a named UniqueConstraint"):
            AddConstraint("Book", constraint, using_index="u")
    with pytest.raises(ConfigurationError, match="using_index must be an index name"):
        AddConstraint("Book", UniqueConstraint(fields=("title",), name="u"), using_index="")


@pytest.mark.asyncio
@pytest.mark.parametrize("atomic", [True, False], ids=["in a transaction", "without a transaction"])
async def test_not_null_is_set_safely(db_simple, atomic):
    tables = SafeTables()
    try:
        state = await tables.create()
        migration = make_migration("0002_title_not_null", AlterColumnNotNullSafe("Book", "title"), atomic=atomic)
        await migration.apply(state.clone(), schema_editor=tables.get_editor(atomic=atomic))
        with pytest.raises(IntegrityError):
            await tables.connection.execute_script(f"INSERT INTO {tables.quote(BOOK_TABLE)} (id) VALUES (3)")
    finally:
        await tables.drop()


@pytest.mark.asyncio
@pytest.mark.parametrize("atomic", [True, False], ids=["in a transaction", "without a transaction"])
async def test_not_null_is_refused_while_a_row_holds_null(db_simple, atomic):
    tables = SafeTables()
    try:
        state = await tables.create()
        await tables.connection.execute_script(f"INSERT INTO {tables.quote(BOOK_TABLE)} (id) VALUES (3)")
        migration = make_migration("0002_title_not_null", AlterColumnNotNullSafe("Book", "title"), atomic=atomic)
        with pytest.raises(ConfigurationError, match="1 row\\(s\\) still have NULL"):
            await migration.apply(state.clone(), schema_editor=tables.get_editor(atomic=atomic))
        # Nothing is left behind - neither NOT NULL nor a CHECK.
        await tables.connection.execute_script(f"INSERT INTO {tables.quote(BOOK_TABLE)} (id) VALUES (4)")
    finally:
        await tables.drop()


def test_the_new_options_are_written_to_a_migration_file_and_read_back():
    operations = [
        AddField("Book", "editor", ForeignKeyField("models.Author", null=True), not_valid=True),
        AddConstraint(
            "Book", UniqueConstraint(fields=("title",), name="safe_book_title"), using_index="safe_book_title"
        ),
    ]
    source = MigrationWriter("0002_safe", "models", operations).as_string()
    migration = Migration.from_source(source, name="0002_safe", app_label="models")
    add_field, add_constraint = migration.operations
    assert isinstance(add_field, AddField) and add_field.not_valid
    assert isinstance(add_constraint, AddConstraint) and add_constraint.using_index == "safe_book_title"
    assert "not_valid" not in AddField("Book", "pages", IntField(null=True)).deconstruct()[2]
    assert add_constraint.describe() == "Add constraint safe_book_title to Book using index safe_book_title"
    assert add_field.describe() == "Add field editor to Book, its foreign key not validated"


def test_the_safety_setting_is_read_and_checked():
    config = HareConfig.load(
        {
            "connections": {"default": "sqlite+aiosqlite://:memory:"},
            "apps": {"models": {"models": ["tests.testmodels"]}},
            "migrations": {"lock_timeout": 5, "safety": {"large_table_rows": 500}},
        }
    )
    assert config.migrations == MigrationsConfig(lock_timeout=5, safety=MigrationSafetyConfig(large_table_rows=500))
    assert config.to_dict()["migrations"] == {"lock_timeout": 5, "safety": {"large_table_rows": 500}}
    assert MigrationsConfig.from_dict({"safety": {}}).safety == MigrationSafetyConfig()
    for bad_value in (-1, 10**12 + 1, 2.5, True, "100"):
        with pytest.raises(ConfigurationError, match="large_table_rows"):
            MigrationsConfig.from_dict({"safety": {"large_table_rows": bad_value}})
    with pytest.raises(ConfigurationError, match="Unknown key 'large_tables'"):
        MigrationsConfig.from_dict({"safety": {"large_tables": 5}})
    with pytest.raises(ConfigurationError, match='"migrations.safety" must be a mapping'):
        MigrationsConfig.from_dict({"safety": 5})
