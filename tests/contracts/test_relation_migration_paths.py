"""Forward and backward migrations remove every column and index of a composite relation."""

import pytest

from hare import fields
from hare.ddl.indexes import Index
from hare.inspectdb.introspection.database_catalog import DatabaseCatalog
from hare.migrations import Migration, MigrationRunner, State
from hare.migrations.operations import AddField, AddIndex, AlterField, CreateModel, RemoveField
from tests.contracts.constants import CONTRACT_APP


@pytest.mark.asyncio
@pytest.mark.parametrize("direction", ("remove", "undo_add"))
@pytest.mark.parametrize("extra_index", ((), ("shelf_code",), ("shelf_code", "shelf_number")))
async def test_removing_a_composite_relation_drops_its_indexes(contract_database, direction, extra_index):
    connection = contract_database.get_connection()
    runner = MigrationRunner(connection)
    initial = Migration(
        "initial",
        CONTRACT_APP,
        operations=[
            CreateModel(
                "Shelf",
                [
                    ("code", fields.CharField(max_length=4)),
                    ("number", fields.IntField()),
                    ("pk", fields.CompositePrimaryKey("code", "number")),
                ],
                options={"table": "relation_shelf"},
            ),
            CreateModel(
                "Book",
                [("id", fields.IntField(primary_key=True, generated=False))],
                options={"table": "relation_book"},
            ),
        ],
    )
    before = await runner.apply(initial, State())
    await connection.execute('INSERT INTO "relation_book" ("id") VALUES (1)')
    operations = [
        AddField("Book", "shelf", fields.ForeignKeyField(f"{CONTRACT_APP}.Shelf", null=True, db_constraint=False))
    ]
    if extra_index:
        if len(extra_index) == 2:
            # The explicit covering index replaces the relation's automatic index. Hand-written
            # operations include the same field alteration the autodetector emits for that change.
            operations.append(
                AlterField(
                    "Book",
                    "shelf",
                    fields.ForeignKeyField(
                        f"{CONTRACT_APP}.Shelf",
                        null=True,
                        db_constraint=False,
                        db_index=False,
                    ),
                )
            )
        operations.append(AddIndex("Book", Index(fields=extra_index, name="book_shelf_code_index")))
    addition = Migration("add_shelf", CONTRACT_APP, operations=operations)
    after = await runner.apply(addition, before)
    added = await DatabaseCatalog.inspect_table(connection, "relation_book")
    assert {column.name for column in added.columns} == {"id", "shelf_code", "shelf_number"}
    assert any(index.columns == ["shelf_code", "shelf_number"] for index in added.indexes)
    if direction == "remove":
        await runner.apply(Migration("remove_shelf", CONTRACT_APP, operations=[RemoveField("Book", "shelf")]), after)
    else:
        await runner.unapply(addition, before)
    removed = await DatabaseCatalog.inspect_table(connection, "relation_book")
    assert {column.name for column in removed.columns} == {"id"}
    assert not [index for index in (*removed.indexes, *removed.column_indexes) if not index.is_unique]
    _, rows = await connection.execute('SELECT "id" FROM "relation_book"')
    assert [row["id"] for row in rows] == [1]
