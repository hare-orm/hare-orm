"""After a field or a table is renamed, its indexes and constraints carry the names hare generates for the
new columns and table - the names a later migration computes for them, never left behind."""

import pytest

from hare.ddl.generated_names import GeneratedNames
from hare.dialects.postgresql.schema.postgresql_schema_editor import PostgresqlSchemaEditor
from hare.dialects.sqlite.drivers.aiosqlite.client import AiosqliteClient
from hare.fields import CharField, ForeignKeyField, IntField
from hare.migrations.migration import Migration
from hare.migrations.operations import AlterModelTable, CreateModel, RenameField
from hare.migrations.state.state import State
from tests.utils.fake_client import FakeClient

pytestmark = pytest.mark.database_independent

APP_LABEL = "models"


def create_item() -> CreateModel:
    return CreateModel(
        name="Item",
        fields=[
            ("id", IntField(primary_key=True)),
            ("price", IntField(db_index=True)),
            ("code", CharField(max_length=20, unique=True)),
        ],
        options={"table": "rn_item"},
    )


async def apply(state: State, editor, *operations) -> None:
    await Migration("rename", APP_LABEL, operations=list(operations)).apply(state, schema_editor=editor)


def get_expected_index_names(state: State, editor) -> set[str]:
    model = state.apps.get_model(f"{APP_LABEL}.Item")
    return {
        index.name for index, _is_constraint in editor.generated_index_names.get_generated_index_names(model).values()
    }


@pytest.mark.asyncio
async def test_indexes_take_the_names_of_the_renamed_columns_and_table_on_sqlite() -> None:
    client = AiosqliteClient(file_path=":memory:", connection_alias="generated_names")
    await client.create_connection(with_db=True)
    try:
        editor = client.dialect.schema_editor_class(client, atomic=True, collect_sql=False)
        state = State()
        await apply(state, editor, create_item())
        await apply(state, editor, RenameField("Item", "price", "amount"), RenameField("Item", "code", "label"))
        _, rows = await client.execute("SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'rn_item'")
        assert {row["name"] for row in rows} == get_expected_index_names(state, editor)

        await apply(state, editor, AlterModelTable("Item", "rn_thing"))
        _, rows = await client.execute("SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'rn_thing'")
        assert {row["name"] for row in rows} == get_expected_index_names(state, editor)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_a_renamed_relation_renames_its_foreign_key_on_postgres() -> None:
    client = FakeClient("postgresql", inline_comment=False)
    editor = PostgresqlSchemaEditor(client)
    state = State()
    await Migration(
        "initial",
        APP_LABEL,
        operations=[
            CreateModel(name="Author", fields=[("id", IntField(primary_key=True))]),
            CreateModel(
                name="Book",
                fields=[("id", IntField(primary_key=True)), ("author", ForeignKeyField(f"{APP_LABEL}.Author"))],
            ),
        ],
    ).apply(state, dry_run=True)

    await apply(state, editor, RenameField("Book", "author", "writer"))

    old_name = GeneratedNames.get_foreign_key_name("book", ("author_id",))
    new_name = GeneratedNames.get_foreign_key_name("book", ("writer_id",))
    assert f'ALTER TABLE "book" RENAME CONSTRAINT "{old_name}" TO "{new_name}"' in client.executed
