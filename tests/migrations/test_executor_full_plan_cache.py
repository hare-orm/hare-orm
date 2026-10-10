from pathlib import Path

import pytest

from hare.core.hare_context import HareContext
from hare.migrations.execution.executor import MigrationExecutor


def _write_migration(migrations_dir: Path, name: str, body_lines: list[str]) -> None:
    (migrations_dir / f"{name}.py").write_text("\n".join(body_lines), encoding="ascii")


def _write_app(tmp_path: Path, app_label: str) -> Path:
    package_dir = tmp_path / app_label
    migrations_dir = package_dir / "migrations"
    migrations_dir.mkdir(parents=True)
    (package_dir / "__init__.py").write_text("", encoding="ascii")
    (migrations_dir / "__init__.py").write_text("", encoding="ascii")
    return migrations_dir


@pytest.mark.asyncio
async def test_collect_sql_on_a_reused_executor_sees_migrations_added_to_disk_after_the_first_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MigrationExecutor._full_plan() caches its result forever on self._full_plan_cache, even
    though self.loader.graph gets rebuilt from disk on every collect_sql()/migrate()/plan() call.
    A migration added to disk between two calls on the SAME executor instance is correctly
    picked up by self.loader.graph, but _project_state() reads the STALE _full_plan_cache -
    confirmed live: RemoveField('subtitle') in 0003 crashed because the state projection never
    replayed 0002's AddField('subtitle') first, even though 0001->0002->0003's dependency chain
    is entirely valid and self.loader.graph.nodes already contained all three."""
    migrations_dir = _write_app(tmp_path, "fullplancacheapp")
    monkeypatch.syspath_prepend(str(tmp_path))

    _write_migration(
        migrations_dir,
        "0001_initial",
        [
            "from hare import migrations",
            "from hare import fields",
            "from hare.migrations import operations as ops",
            "",
            "class Migration(migrations.Migration):",
            "    dependencies = []",
            "    operations = [",
            "        ops.CreateModel(",
            "            name='Post',",
            "            fields=[",
            "                ('id', fields.IntField(generated=True, primary_key=True)),",
            "                ('title', fields.CharField(max_length=100)),",
            "            ],",
            "            options={'table': 'post'},",
            "        ),",
            "    ]",
            "",
        ],
    )

    async with HareContext() as ctx:
        ctx.connections._init_config(
            {
                "default": {
                    "engine": "sqlite+aiosqlite",
                    "credentials": {"file_path": str(tmp_path / "full_plan_cache.sqlite3")},
                }
            }
        )
        apps_config = {
            "fullplancacheapp": {
                "models": [],
                "default_connection": "default",
                "migrations": "fullplancacheapp.migrations",
            }
        }
        connection = ctx.connections.get("default")
        executor = MigrationExecutor(connection, apps_config)

        # Populates _full_plan_cache with just 0001_initial.
        await executor.collect_sql("fullplancacheapp", "0001_initial")

        # New migrations appear on disk AFTER the first call, on the SAME executor instance -
        # the natural shape for a long-lived service/worker process that holds one executor
        # alive between checks, rather than constructing a fresh one every time (as the CLI does).
        _write_migration(
            migrations_dir,
            "0002_addfield",
            [
                "from hare import migrations",
                "from hare import fields",
                "from hare.migrations import operations as ops",
                "",
                "class Migration(migrations.Migration):",
                "    dependencies = [('fullplancacheapp', '0001_initial')]",
                "    operations = [",
                "        ops.AddField(",
                "            model_name='Post',",
                "            name='subtitle',",
                "            field=fields.CharField(max_length=100, null=True),",
                "        ),",
                "    ]",
                "",
            ],
        )
        _write_migration(
            migrations_dir,
            "0003_removefield",
            [
                "from hare import migrations",
                "from hare.migrations import operations as ops",
                "",
                "class Migration(migrations.Migration):",
                "    dependencies = [('fullplancacheapp', '0002_addfield')]",
                "    operations = [",
                "        ops.RemoveField(",
                "            model_name='Post',",
                "            name='subtitle',",
                "        ),",
                "    ]",
                "",
            ],
        )

        # Must not raise - self.loader.graph already sees all three migrations, and
        # _full_plan()'s cache must be invalidated alongside it.
        removing_sql = await executor.collect_sql("fullplancacheapp", "0003_removefield")
        assert any("subtitle" in statement for statement in removing_sql)

        await connection.close()
