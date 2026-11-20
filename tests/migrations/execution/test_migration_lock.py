"""migrate holds the dialect's migration lock for its whole run - a second one waits for it."""

import asyncio
import contextvars

import pytest

from hare.core.connections.connections import Connections
from hare.core.resource_completion import ResourceCompletion
from hare.migrations.execution.connections_plan import ConnectionsPlan
from hare.migrations.execution.executor import MigrationExecutor
from tests.utils.migration_lock_waiter import MigrationLockWaiter


def write_migrations(tmp_path, app_label: str) -> str:
    package = tmp_path / app_label
    (package / "migrations").mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="ascii")
    (package / "migrations" / "__init__.py").write_text("", encoding="ascii")
    (package / "migrations" / "0001_initial.py").write_text(
        "\n".join(
            [
                "from hare import migrations",
                "from hare.migrations import operations as ops",
                "",
                "class Migration(migrations.Migration):",
                "    initial = True",
                "    operations = [",
                f'        ops.RunSQL(\'CREATE TABLE "{app_label}_widget" ("id" INT PRIMARY KEY)\'),',
                "    ]",
                "",
            ]
        ),
        encoding="ascii",
    )
    return f"{app_label}.migrations"


@pytest.mark.asyncio
async def test_migrate_waits_for_the_migration_lock(db_isolated_no_schema, tmp_path, monkeypatch):
    connection = db_isolated_no_schema.get_connection()
    lock_sql = connection.dialect.schema_editor_class.table_locks_class.get_migration_lock_sql()
    if lock_sql is None:
        pytest.skip("The dialect has no migration lock")
    app_label = "migrationlockapp"
    monkeypatch.syspath_prepend(str(tmp_path))
    apps_config = {
        app_label: {
            "models": [],
            "default_connection": connection.connection_alias,
            "migrations": write_migrations(tmp_path, app_label),
        }
    }
    # The concurrent migrate runs as another process would - outside the holder's transaction.
    migrate_context = contextvars.copy_context()
    holder = Connections.current().create_independent(connection.connection_alias)
    migrate = None
    try:
        async with holder._in_transaction() as holder_client:
            await holder_client.execute(lock_sql)
            waiter = MigrationLockWaiter(holder_client, lock_sql, monkeypatch)
            migrate = asyncio.create_task(
                MigrationExecutor(connection, apps_config).migrate(), context=migrate_context
            )
            await waiter.wait(migrate)
        await asyncio.wait_for(migrate, timeout=30)
    finally:
        try:
            if migrate is not None:
                if not migrate.done():
                    migrate.cancel()
                await ResourceCompletion.wait(asyncio.gather(migrate, return_exceptions=True))
        finally:
            await ResourceCompletion.wait(holder.close())

    assert await connection.execute_dicts(f'SELECT * FROM "{app_label}_widget"') == []


@pytest.mark.asyncio
async def test_a_migrate_waiting_for_the_lock_leaves_out_what_the_holder_applied(
    db_isolated_no_schema, tmp_path, monkeypatch
):
    """A plan made before the lock ran again the migrations another migrate applied while this one
    waited - its CREATE TABLE failed on the table the other one made."""
    connection = db_isolated_no_schema.get_connection()
    lock_sql = connection.dialect.schema_editor_class.table_locks_class.get_migration_lock_sql()
    if lock_sql is None:
        pytest.skip("The dialect has no migration lock")
    app_label = "migrationlockplanapp"
    monkeypatch.syspath_prepend(str(tmp_path))
    apps_config = {
        app_label: {
            "models": [],
            "default_connection": connection.connection_alias,
            "migrations": write_migrations(tmp_path, app_label),
        }
    }
    executor = MigrationExecutor(connection, apps_config)
    await executor.runner.ensure_journal()
    reported: list[list[str]] = []

    def reporter(alias, steps, fake, dry_run):
        reported.append([step.migration.name for step in steps])

    migrate_context = contextvars.copy_context()
    holder = Connections.current().create_independent(connection.connection_alias)
    migrate = None
    try:
        async with holder._in_transaction() as holder_client:
            await holder_client.execute(lock_sql)
            waiter = MigrationLockWaiter(holder_client, lock_sql, monkeypatch)
            migrate = asyncio.create_task(
                ConnectionsPlan({connection.connection_alias: executor}).migrate(reporter=reporter),
                context=migrate_context,
            )
            await waiter.wait(migrate)
            # The holder applies the migration, as another process's migrate would.
            await holder_client.execute_script(f'CREATE TABLE "{app_label}_widget" ("id" INT PRIMARY KEY)')
            await executor.recorder.record_applied(app_label, "0001_initial", connection=holder_client)
        await asyncio.wait_for(migrate, timeout=30)
    finally:
        try:
            if migrate is not None:
                if not migrate.done():
                    migrate.cancel()
                await ResourceCompletion.wait(asyncio.gather(migrate, return_exceptions=True))
        finally:
            await ResourceCompletion.wait(holder.close())

    assert reported == [[]]
    assert await connection.execute_dicts(f'SELECT * FROM "{app_label}_widget"') == []
