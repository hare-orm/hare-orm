"""RenameField of a field another model's composite relation reads, two relations deep: a relation
naming the renamed key fields in its to_field follows them - for a renamed key component of the first
model and for the renamed relation itself, whose key fields are named after it."""

import pytest

from hare import fields
from hare.dialects.sqlite.drivers.aiosqlite.client import AiosqliteClient
from hare.migrations.migration import Migration
from hare.migrations.operations import CreateModel, RenameField
from hare.migrations.state.state import State

pytestmark = pytest.mark.sqlite_only


def get_create_operations() -> list[CreateModel]:
    return [
        CreateModel(
            name="Parent",
            fields=[
                ("a", fields.IntField()),
                ("b", fields.CharField(max_length=20)),
                ("pk", fields.CompositePrimaryKey("a", "b")),
            ],
            options={"table": "chain_parent"},
        ),
        CreateModel(
            name="Edition",
            fields=[
                ("parent", fields.ForeignKeyField("models.Parent", related_name="editions")),
                ("number", fields.IntField()),
                ("pk", fields.CompositePrimaryKey("parent", "number")),
            ],
            options={"table": "chain_edition"},
        ),
        CreateModel(
            name="Note",
            fields=[
                (
                    "edition",
                    # As makemigrations writes a relation to a composite key.
                    fields.ForeignKeyField(
                        "models.Edition", related_name="notes", to_field=("parent_a", "parent_b", "number")
                    ),
                ),
                ("position", fields.IntField()),
                ("pk", fields.CompositePrimaryKey("edition", "position")),
            ],
            options={"table": "chain_note"},
        ),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("renamed", "note_key_columns"),
    [
        (("Parent", "a", "aa"), ["edition_number", "edition_parent_aa", "edition_parent_b"]),
        (("Edition", "parent", "owner"), ["edition_number", "edition_owner_a", "edition_owner_b"]),
    ],
)
async def test_a_rename_reaches_the_relations_reading_the_renamed_key(renamed, note_key_columns):
    client = AiosqliteClient(file_path=":memory:", connection_alias="rename_chain")
    await client.create_connection(with_db=True)
    try:
        editor = client.dialect.schema_editor_class(client, atomic=True, collect_sql=False)
        state = await Migration("0001", "models", operations=get_create_operations()).apply(
            State(), schema_editor=editor
        )
        await client.execute_script(
            "INSERT INTO chain_parent (a, b) VALUES (1, 'x');"
            "INSERT INTO chain_edition (parent_a, parent_b, number) VALUES (1, 'x', 7);"
            "INSERT INTO chain_note (edition_parent_a, edition_parent_b, edition_number, position) "
            "VALUES (1, 'x', 7, 3)"
        )
        state = await Migration("0002", "models", operations=[RenameField(*renamed)]).apply(
            state, schema_editor=editor
        )
        note = state.apps.get_model("models.Note")
        assert sorted(column for name, column in note._meta.fields_db_projection.items() if name != "position") == (
            note_key_columns
        )
        rows = await client.execute_dicts(f"SELECT {', '.join(note_key_columns)}, position FROM chain_note")
        assert rows == [dict(zip([*note_key_columns, "position"], [7, 1, "x", 3], strict=True))]
    finally:
        await client.close()
