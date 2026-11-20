"""SQLite migrations preserve the connection and constraint scope borrowed from their caller."""

import asyncio
from dataclasses import replace

import pytest

from hare import fields
from hare.dialects.base.schema.plans.enums import SchemaEffect
from hare.dialects.base.schema.plans.schema_change_plan import SchemaChangePlan
from hare.dialects.base.schema.plans.schema_statement import SchemaStatement
from hare.dialects.base.schema.plans.schema_transaction import SchemaTransaction
from hare.dialects.sqlite.client.sqlite_pragma_scope import SqlitePragmaScope
from hare.dialects.sqlite.drivers.aiosqlite.client.aiosqlite_client import AiosqliteClient
from hare.exceptions import IntegrityError, TransactionManagementError
from hare.migrations import Migration, MigrationRunner, State
from hare.migrations.operations import AlterField, CreateModel, RunSQL
from tests.contracts.constants import CONTRACT_APP


@pytest.mark.asyncio
async def test_direct_migration_replacement_preserves_referencing_rows(contract_database):
    connection = contract_database.get_connection()
    if connection.dialect.name != "sqlite":
        pytest.skip("SQLite replacement constraint scope")
    initial = Migration(
        "initial",
        CONTRACT_APP,
        operations=[
            CreateModel("Parent", [("id", fields.IntField(primary_key=True)), ("label", fields.TextField())]),
            CreateModel(
                "Child",
                [
                    ("id", fields.IntField(primary_key=True)),
                    ("parent", fields.ForeignKeyField(f"{CONTRACT_APP}.Parent")),
                ],
            ),
        ],
    )
    before = await MigrationRunner(connection).apply(
        initial, State(default_connections={CONTRACT_APP: connection.connection_alias})
    )
    parent_apps = before.apps
    parent = parent_apps.get_model(f"{CONTRACT_APP}.Parent")
    child_apps = before.apps
    child = child_apps.get_model(f"{CONTRACT_APP}.Child")
    obj = await parent_apps.get_queryset(parent).create(label="retained")
    await child_apps.get_queryset(child).create(parent_id=obj.pk)
    change = Migration(
        "nullable", CONTRACT_APP, operations=[AlterField("Parent", "label", fields.TextField(null=True))]
    )
    editor = connection.dialect.schema_editor_class(connection, atomic=False)
    await change.apply(before, schema_editor=editor)
    assert await child_apps.get_queryset(child).count() == 1
    await change.unapply(before, schema_editor=editor)
    assert await child_apps.get_queryset(child).count() == 1
    assert await parent_apps.get_queryset(parent).values_list("label", flat=True) == ["retained"]
    assert await connection.read_foreign_key_state()


@pytest.mark.asyncio
async def test_replacement_plan_checks_disabled_constraints_before_its_commit(contract_database):
    connection = contract_database.get_connection()
    if connection.dialect.name != "sqlite":
        pytest.skip("SQLite replacement constraint scope")
    await connection.execute_script(
        "CREATE TABLE plan_parent (id INTEGER PRIMARY KEY);"
        "CREATE TABLE plan_child (id INTEGER REFERENCES plan_parent(id));"
    )
    writes = SchemaChangePlan(
        (SchemaStatement("INSERT INTO plan_child VALUES (7)", frozenset({SchemaEffect.REPLACE_TABLE})),)
    )
    plan = SchemaChangePlan((SchemaTransaction(writes),))
    editor = connection.dialect.schema_editor_class(connection)
    with pytest.raises(IntegrityError, match="foreign key violation"):
        await plan.execute(editor)
    assert await connection.execute_dicts("SELECT id FROM plan_child") == []
    assert await connection.read_foreign_key_state()


@pytest.mark.asyncio
@pytest.mark.parametrize("atomic", [False, True])
@pytest.mark.parametrize("backward", [False, True], ids=["apply", "unapply"])
async def test_direct_migration_checks_constraints_inside_its_actual_transaction(contract_database, atomic, backward):
    connection = contract_database.get_connection()
    if connection.dialect.name != "sqlite":
        pytest.skip("SQLite migration constraint scope")
    await connection.execute_script(
        "CREATE TABLE direct_parent (id INTEGER PRIMARY KEY);"
        "CREATE TABLE direct_child (id INTEGER REFERENCES direct_parent(id));"
    )
    insert = "INSERT INTO direct_child VALUES (7)"
    migration = Migration(
        "direct_write",
        CONTRACT_APP,
        operations=[
            RunSQL("SELECT 1" if backward else insert, insert if backward else "SELECT 1"),
        ],
    )
    editor = connection.dialect.schema_editor_class(connection, atomic=atomic)
    run = migration.unapply if backward else migration.apply
    with pytest.raises(IntegrityError, match="foreign key violation"):
        await run(State(default_connections={CONTRACT_APP: connection.connection_alias}), schema_editor=editor)
    assert await connection.execute_dicts("SELECT id FROM direct_child") == []
    assert editor.client is connection
    assert await connection.read_foreign_key_state()


@pytest.mark.asyncio
@pytest.mark.parametrize("transaction_argument", [False, True], ids=["base", "transaction"])
@pytest.mark.parametrize("backward", [False, True], ids=["apply", "unapply"])
async def test_migration_borrows_transaction_without_committing(contract_database, transaction_argument, backward):
    connection = contract_database.get_connection()
    if connection.dialect.name != "sqlite":
        pytest.skip("SQLite constraint scope")
    await connection.execute_script("CREATE TABLE borrowed_rows (id INTEGER)")
    migration = Migration(
        "borrowed_write",
        CONTRACT_APP,
        operations=[RunSQL("INSERT INTO borrowed_rows VALUES (7)", "DELETE FROM borrowed_rows WHERE id = 7")],
    )
    state = State(default_connections={CONTRACT_APP: connection.connection_alias})
    if backward:
        state = await MigrationRunner(connection).apply(migration, state)
    async with connection._in_transaction() as transaction:
        runner = MigrationRunner(transaction if transaction_argument else connection)
        run = runner.unapply if backward else runner.apply
        await run(migration, state)
        assert await transaction.execute_dicts("SELECT id FROM borrowed_rows") == ([] if backward else [{"id": 7}])
        await transaction.rollback()
    assert await connection.execute_dicts("SELECT id FROM borrowed_rows") == ([{"id": 7}] if backward else [])


@pytest.mark.asyncio
@pytest.mark.parametrize("transaction_argument", [False, True], ids=["base", "transaction"])
async def test_borrowed_migration_preserves_deferred_foreign_keys(contract_database, transaction_argument):
    connection = contract_database.get_connection()
    if connection.dialect.name != "sqlite":
        pytest.skip("SQLite constraint scope")
    await connection.execute_script(
        "CREATE TABLE borrowed_parent (id INTEGER PRIMARY KEY);"
        "CREATE TABLE borrowed_child (id INTEGER REFERENCES borrowed_parent(id));"
        "CREATE TABLE borrowed_marker (id INTEGER);"
    )
    async with connection._in_transaction() as transaction:
        await transaction.execute("PRAGMA defer_foreign_keys = ON")
        await transaction.execute("INSERT INTO borrowed_child VALUES (7)")
        migration = Migration(
            "deferred_write", CONTRACT_APP, operations=[RunSQL("INSERT INTO borrowed_marker VALUES (1)")]
        )
        await MigrationRunner(transaction if transaction_argument else connection).apply(
            migration, State(default_connections={CONTRACT_APP: connection.connection_alias})
        )
        await transaction.execute("INSERT INTO borrowed_parent VALUES (7)")
    assert await connection.execute_dicts("SELECT id FROM borrowed_child") == [{"id": 7}]
    assert (await connection.execute("PRAGMA foreign_keys")).rows[0][0] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("wrapped", [False, True], ids=["plain", "transaction-step"])
async def test_replacement_plan_refuses_enabled_foreign_keys_before_any_step(contract_database, wrapped):
    connection = contract_database.get_connection()
    if connection.dialect.name != "sqlite":
        pytest.skip("SQLite replacement plans")
    state = State(default_connections={CONTRACT_APP: connection.connection_alias})
    initial = Migration(
        "initial",
        CONTRACT_APP,
        operations=[CreateModel("Entry", [("id", fields.IntField(primary_key=True)), ("label", fields.TextField())])],
    )
    state = await MigrationRunner(connection).apply(initial, state)
    target = state.clone()
    AlterField("Entry", "label", fields.TextField(null=True)).state_forward(CONTRACT_APP, target)
    await connection.execute_script("CREATE TABLE replacement_marker (id INTEGER)")
    async with connection._in_transaction() as transaction:
        editor = transaction.dialect.schema_editor_class(transaction)
        replacement = editor.table_rebuild.get_rebuild_plan(
            state.apps.get_binding(f"{CONTRACT_APP}.Entry"), target.apps.get_binding(f"{CONTRACT_APP}.Entry")
        )
        body = SchemaChangePlan((SchemaStatement("INSERT INTO replacement_marker VALUES (1)"), *replacement.steps))
        plan = SchemaChangePlan((SchemaTransaction(body),)) if wrapped else body
        with pytest.raises(TransactionManagementError, match="foreign_keys"):
            await plan.execute(editor)
        assert await transaction.execute_dicts("SELECT id FROM replacement_marker") == []
        assert editor.run_statement_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("configured", [False, True], ids=["configured-off", "configured-on"])
async def test_foreign_key_state_tracks_physical_scope_in_inherited_tasks(contract_database, configured):
    connection = AiosqliteClient(connection_alias="scope_state", file_path=":memory:", foreign_keys=configured)
    try:
        await connection.create_connection(with_db=True)
        assert connection.enforces_foreign_keys() is configured
        editor = connection.dialect.schema_editor_class(connection)
        released = asyncio.Event()

        async def read_after_release():
            await released.wait()
            return connection.enforces_foreign_keys(), (await connection.execute("PRAGMA foreign_keys")).rows[0][0]

        async with editor.constraint_checking_disabled():
            assert not connection.enforces_foreign_keys()
            child = asyncio.create_task(read_after_release())
        released.set()
        assert await child == (configured, int(configured))
        async with connection._in_transaction() as transaction:
            async with SqlitePragmaScope.run(transaction, "foreign_keys", "OFF"):
                assert transaction.enforces_foreign_keys() is configured
                assert (await transaction.execute("PRAGMA foreign_keys")).rows[0][0] == int(configured)
        await connection.close()
        await connection.create_connection(with_db=True)
        assert connection.enforces_foreign_keys() is configured
    finally:
        await connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("previous", [0, 1], ids=["actual-off", "actual-on"])
async def test_constraint_scope_restores_actual_setting_after_failure(contract_database, previous):
    connection = contract_database.get_connection()
    if connection.dialect.name != "sqlite":
        pytest.skip("SQLite constraint scope")
    await connection.execute(f"PRAGMA foreign_keys = {previous}")
    editor = connection.dialect.schema_editor_class(connection)
    with pytest.raises(ValueError, match="scope failure"):
        async with editor.constraint_checking_disabled():
            assert not connection.enforces_foreign_keys()
            raise ValueError("scope failure")
    assert (await connection.execute("PRAGMA foreign_keys")).rows[0][0] == previous
    assert connection.enforces_foreign_keys() is bool(previous)


@pytest.mark.asyncio
async def test_constraint_scope_cancellation_restores_only_its_physical_client(contract_database):
    connection = contract_database.get_connection()
    if connection.dialect.name != "sqlite":
        pytest.skip("SQLite physical connection state")
    independent = connection.create_independent_client()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def hold_scope():
        async with connection.dialect.schema_editor_class(connection).constraint_checking_disabled():
            assert independent.enforces_foreign_keys()
            entered.set()
            await release.wait()

    task = None
    try:
        await independent.create_connection(with_db=True)
        task = asyncio.create_task(hold_scope())
        await asyncio.wait_for(entered.wait(), 10)
        assert not connection.enforces_foreign_keys()
        assert independent.enforces_foreign_keys()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await connection.read_foreign_key_state()
        assert independent.enforces_foreign_keys()
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await independent.close()


@pytest.mark.asyncio
async def test_foreign_key_state_stays_on_pinned_transaction_after_context_replacement(contract_database):
    context = contract_database
    previous = context.get_connection()
    if previous.dialect.name != "sqlite":
        pytest.skip("SQLite physical connection state")
    if previous.holds_database_in_connection():
        pytest.skip("A new private memory connection has a different database")
    await previous.execute_script("CREATE TABLE pinned_rows (id INTEGER); INSERT INTO pinned_rows VALUES (1)")
    await previous.execute("PRAGMA foreign_keys = OFF")
    assert not await previous.read_foreign_key_state()
    configuration = replace(
        previous.connection_config,
        credentials={**previous.connection_config.credentials, "foreign_keys": True},
    )
    closing = asyncio.Event()
    close = previous.close

    async def observed_close():
        closing.set()
        await close()

    previous.close = observed_close
    replacing = None
    try:
        async with previous._in_transaction() as transaction:
            replacing = asyncio.create_task(
                context.init(
                    config={
                        "connections": {previous.connection_alias: configuration.to_config()},
                        "apps": {},
                    }
                )
            )
            await asyncio.wait_for(closing.wait(), 10)
            current = context.get_connection(previous.connection_alias)
            assert current is not previous
            assert await current.read_foreign_key_state()
            assert not transaction.enforces_foreign_keys()
            async with transaction._in_transaction() as nested:
                assert not await nested.read_foreign_key_state()
                assert not nested.enforces_foreign_keys()
            assert current.enforces_foreign_keys()
            assert previous.enforces_foreign_keys()
            assert not await transaction.read_foreign_key_state()
            assert current.enforces_foreign_keys()
            await transaction.execute("INSERT INTO pinned_rows VALUES (2)")
    finally:
        if replacing is not None:
            await asyncio.wait_for(replacing, 20)
    assert await context.get_connection(previous.connection_alias).execute_dicts(
        "SELECT id FROM pinned_rows ORDER BY id"
    ) == [{"id": 1}, {"id": 2}]
