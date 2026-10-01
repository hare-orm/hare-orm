"""migrate holds the dialect's migration lock for its whole run - a second one waits for it."""

import asyncio
import contextvars

import pytest

from hare.core.connections import Connections
from hare.migrations.execution.executor import MigrationExecutor


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
    connection = db_isolated_no_schema.db()
    lock_sql = connection.dialect.get_migration_lock_sql()
    if lock_sql is None:
        pytest.skip("The dialect has no migration lock")
    app_label = "migrationlockapp"
    monkeypatch.syspath_prepend(str(tmp_path))
    apps_config = {
        app_label: {
            "models": [],
            "default_connection": connection.connection_name,
            "migrations": write_migrations(tmp_path, app_label),
        }
    }
    # The concurrent migrate runs as another process would - outside the holder's transaction.
    migrate_context = contextvars.copy_context()
    holder = Connections.current().create_independent(connection.connection_name)
    try:
        async with holder._in_transaction() as holder_client:
            await holder_client.execute(lock_sql)
            migrate = asyncio.create_task(
                MigrationExecutor(connection, apps_config).migrate(), context=migrate_context
            )
            await asyncio.sleep(0.5)
            assert not migrate.done()
        await asyncio.wait_for(migrate, timeout=30)
    finally:
        await holder.close()

    assert await connection.execute_dicts(f'SELECT * FROM "{app_label}_widget"') == []
