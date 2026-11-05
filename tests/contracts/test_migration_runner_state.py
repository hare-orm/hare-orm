"""Public migration runs preserve the caller's before/after snapshots."""

import pytest

from hare import fields
from hare.inspectdb.introspection.database_catalog import DatabaseCatalog
from hare.migrations import Migration, MigrationRunner, State
from hare.migrations.exceptions import IncompatibleStateError
from hare.migrations.operations import AddField, CreateModel, RemoveField
from tests.contracts.constants import CONTRACT_APP


@pytest.mark.asyncio
async def test_a_dry_run_and_apply_keep_the_state_needed_to_undo(contract_database):
    connection = contract_database.get_connection()
    runner = MigrationRunner(connection)
    before = State()
    migration = Migration(
        "create_book",
        CONTRACT_APP,
        operations=[
            CreateModel(
                "Book",
                [("id", fields.IntField(primary_key=True, generated=False))],
                options={"table": "snapshot_book"},
            )
        ],
    )
    preview = await runner.apply(migration, before, dry_run=True)
    assert before.models == {}
    assert (CONTRACT_APP, "Book") in preview.models
    assert not await DatabaseCatalog.table_exists(connection, "snapshot_book")
    after = await runner.apply(migration, before)
    assert before.models == {}
    assert await DatabaseCatalog.table_exists(connection, "snapshot_book")
    preview_undo = await runner.unapply(migration, state_before=before, dry_run=True)
    assert preview_undo.models == {}
    assert (CONTRACT_APP, "Book") in after.models
    assert await DatabaseCatalog.table_exists(connection, "snapshot_book")
    restored = await runner.unapply(migration, state_before=before)
    assert restored is not before
    assert restored.models == before.models == {}
    assert (CONTRACT_APP, "Book") in after.models
    assert not await DatabaseCatalog.table_exists(connection, "snapshot_book")


@pytest.mark.asyncio
async def test_a_failed_preview_preserves_existing_model_fields(contract_database):
    runner = MigrationRunner(contract_database.get_connection())
    before = await runner.apply(
        Migration(
            "initial",
            CONTRACT_APP,
            operations=[
                CreateModel(
                    "Book",
                    [("id", fields.IntField(primary_key=True))],
                )
            ],
        ),
        State(),
        dry_run=True,
    )
    original = before.models[(CONTRACT_APP, "Book")]
    with pytest.raises(IncompatibleStateError):
        await runner.apply(
            Migration(
                "invalid",
                CONTRACT_APP,
                operations=[
                    AddField("Book", "title", fields.CharField(max_length=20)),
                    RemoveField("Book", "missing"),
                ],
            ),
            before,
            dry_run=True,
        )
    assert before.models[(CONTRACT_APP, "Book")] is original
    assert set(original.fields) == {"id"}
