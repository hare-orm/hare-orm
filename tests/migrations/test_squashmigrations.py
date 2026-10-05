"""squashmigrations: a run of an app's migrations squashed into one that the loader puts in their
place where none or all of them are applied, and leaves out until all are."""

import importlib
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest

from hare import Hare
from hare.exceptions import ConfigurationError
from hare.migrations.api import migrate, squashmigrations
from hare.migrations.exceptions import MigrationLoadError
from hare.migrations.operations import AddField, CreateModel, RemoveField, RunPython, RunSQL

INITIAL = """
from hare import fields, migrations
from hare.migrations import operations as ops


class Migration(migrations.Migration):
    operations = [
        ops.CreateModel(
            name="Widget",
            fields=[("id", fields.IntField(primary_key=True)), ("name", fields.CharField(max_length=50))],
            options={"table": "widget"},
        ),
    ]
"""
ADD_SIZE = """
from hare import fields, migrations
from hare.migrations import operations as ops


class Migration(migrations.Migration):
    dependencies = [("app", "0001_initial")]
    operations = [ops.AddField(model_name="Widget", name="size", field=fields.IntField(default=0))]
"""
SEED = """
import json

from hare import migrations
from hare.migrations import operations as ops


async def seed_widgets(apps, schema_editor):
    widget_model = apps.get_model("app", "Widget")
    for number in json.loads("[1, 2]"):
        await widget_model.objects.create(id=number, name=f"seeded-{number}")


async def unseed_widgets(apps, schema_editor):
    await apps.get_model("app", "Widget").objects.filter(id__in=[1, 2]).delete()


class Migration(migrations.Migration):
    dependencies = [("app", "0002_add_size")]
    operations = [
        ops.RunPython(seed_widgets, unseed_widgets),
        ops.RunSQL("SELECT 1", "SELECT 1", elidable=True),
    ]
"""
REMOVE_NAME_ADD_COLOR = """
from hare import fields, migrations
from hare.migrations import operations as ops


class Migration(migrations.Migration):
    dependencies = [("app", "0003_seed")]
    operations = [
        ops.AddField(model_name="Widget", name="color", field=fields.CharField(max_length=20, default="red")),
        ops.AlterField(model_name="Widget", name="color", field=fields.CharField(max_length=30, default="blue")),
    ]
"""
ADD_NOTE = """
from hare import fields, migrations
from hare.migrations import operations as ops


class Migration(migrations.Migration):
    dependencies = [("app", "0004_color")]
    operations = [ops.AddField(model_name="Widget", name="note", field=fields.TextField(null=True))]
"""
MIGRATIONS = {
    "0001_initial": INITIAL,
    "0002_add_size": ADD_SIZE,
    "0003_seed": SEED,
    "0004_color": REMOVE_NAME_ADD_COLOR,
    "0005_note": ADD_NOTE,
}
OTHER_APP_INITIAL = """
from hare import fields, migrations
from hare.migrations import operations as ops


class Migration(migrations.Migration):
    dependencies = [("app", "0003_seed")]
    operations = [
        ops.CreateModel(
            name="Gadget",
            fields=[
                ("id", fields.IntField(primary_key=True)),
                ("widget", fields.ForeignKeyField("app.Widget", related_name="gadgets")),
            ],
            options={"table": "gadget"},
        ),
    ]
"""


class SquashProject:
    """Two apps on disk - ``app`` with the migrations above, ``other`` depending on one of them -
    on a SQLite file database."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, with_other_app: bool = False) -> None:
        self.tmp_path = tmp_path
        self.package_name = f"squash_app_{tmp_path.name}"
        self.other_package_name = f"squash_other_{tmp_path.name}"
        self.with_other_app = with_other_app
        self.migrations_path = self.write_package(self.package_name, MIGRATIONS)
        if with_other_app:
            self.write_package(self.other_package_name, {"0001_initial": OTHER_APP_INITIAL})
        monkeypatch.syspath_prepend(str(tmp_path))
        importlib.invalidate_caches()

    def write_package(self, package_name: str, migrations: dict[str, str]) -> Path:
        package_path = self.tmp_path / package_name
        migrations_path = package_path / "migrations"
        migrations_path.mkdir(parents=True)
        (package_path / "__init__.py").write_text("", encoding="utf-8")
        (package_path / "models.py").write_text("", encoding="utf-8")
        (migrations_path / "__init__.py").write_text("", encoding="utf-8")
        for name, source in migrations.items():
            (migrations_path / f"{name}.py").write_text(source.lstrip(), encoding="utf-8")
        return migrations_path

    @property
    def database_path(self) -> Path:
        return self.tmp_path / "db.sqlite3"

    @property
    def config(self) -> dict[str, Any]:
        apps = {
            "app": {
                "models": [f"{self.package_name}.models"],
                "default_connection": "default",
                "migrations": f"{self.package_name}.migrations",
            }
        }
        if self.with_other_app:
            apps["other"] = {
                "models": [f"{self.other_package_name}.models"],
                "default_connection": "default",
                "migrations": f"{self.other_package_name}.migrations",
            }
        return {
            "connections": {
                "default": {"engine": "sqlite+aiosqlite", "credentials": {"file_path": str(self.database_path)}}
            },
            "apps": apps,
        }

    async def migrate(self, target: str | None = None) -> None:
        try:
            await migrate(config=self.config, target=target)
        finally:
            await Hare.close_connections()

    async def squash(self, end_name: str, start_name: str | None = None, squashed_name: str | None = None) -> str:
        squashed = await squashmigrations(
            config=self.config, app_label="app", end_name=end_name, start_name=start_name, squashed_name=squashed_name
        )
        assert squashed.writer is not None
        squashed.writer.write()
        for module_name in list(sys.modules):
            if module_name.startswith(f"{self.package_name}.migrations."):
                del sys.modules[module_name]
        importlib.invalidate_caches()
        return squashed.writer.name

    def delete_migrations(self, *names: str) -> None:
        for name in names:
            (self.migrations_path / f"{name}.py").unlink()
            sys.modules.pop(f"{self.package_name}.migrations.{name}", None)
        importlib.invalidate_caches()

    def journal(self) -> set[tuple[str, str]]:
        with sqlite3.connect(self.database_path) as connection:
            return set(connection.execute("SELECT app, name FROM hare_migrations").fetchall())

    def columns(self, table: str) -> list[str]:
        with sqlite3.connect(self.database_path) as connection:
            return [row[1] for row in connection.execute(f"PRAGMA table_info({table})").fetchall()]

    def widget_names(self) -> list[str]:
        with sqlite3.connect(self.database_path) as connection:
            return [row[0] for row in connection.execute("SELECT name FROM widget ORDER BY id").fetchall()]

    def tables(self) -> set[str]:
        with sqlite3.connect(self.database_path) as connection:
            return {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


ALL_APP_MIGRATIONS = {("app", name) for name in MIGRATIONS}
FINAL_WIDGET_COLUMNS = ["id", "name", "size", "color", "note"]


@pytest.mark.asyncio
async def test_squashed_migration_shortens_operations_and_keeps_the_data_migration(tmp_path, monkeypatch):
    project = SquashProject(tmp_path, monkeypatch)
    squashed = await squashmigrations(config=project.config, app_label="app", end_name="0005")
    writer = squashed.writer
    assert writer is not None
    assert writer.name == "0001_squashed_0005_note"
    assert writer.replaces == [("app", name) for name in MIGRATIONS]
    assert writer.dependencies == []
    assert writer.initial is True
    assert squashed.elided_operation_count == 1
    assert squashed.squashed_operation_count == 6
    # The CreateModel takes in the AddField that comes before the RunPython; the ones after it stay.
    assert [type(operation) for operation in writer.operations] == [CreateModel, RunPython, AddField, AddField]
    assert [name for name, _field in writer.operations[0].fields] == ["id", "name", "size"]
    assert writer.operations[2].field.max_length == 30
    source = writer.as_string()
    assert "async def seed_widgets(apps, schema_editor):" in source
    assert "import json" in source
    assert "RunSQL" not in source
    assert "squash_app_" not in source


@pytest.mark.asyncio
async def test_a_fresh_database_runs_the_squashed_migration_in_place_of_the_replaced_ones(tmp_path, monkeypatch):
    project = SquashProject(tmp_path, monkeypatch)
    squashed_name = await project.squash("0005")
    await project.migrate()
    assert project.journal() == ALL_APP_MIGRATIONS | {("app", squashed_name)}
    assert project.columns("widget") == FINAL_WIDGET_COLUMNS
    assert project.widget_names() == ["seeded-1", "seeded-2"]
    # Nothing left to apply.
    await project.migrate()
    assert project.widget_names() == ["seeded-1", "seeded-2"]


@pytest.mark.asyncio
async def test_a_database_with_every_replaced_migration_applied_records_the_squashed_one(tmp_path, monkeypatch):
    project = SquashProject(tmp_path, monkeypatch)
    await project.migrate()
    squashed_name = await project.squash("0005")
    await project.migrate()
    assert project.journal() == ALL_APP_MIGRATIONS | {("app", squashed_name)}
    assert project.widget_names() == ["seeded-1", "seeded-2"]


@pytest.mark.asyncio
async def test_a_database_with_some_replaced_migrations_applied_runs_the_rest_of_them(tmp_path, monkeypatch):
    project = SquashProject(tmp_path, monkeypatch)
    await project.migrate(target="app.0002_add_size")
    squashed_name = await project.squash("0005")
    await project.migrate()
    assert project.journal() == ALL_APP_MIGRATIONS | {("app", squashed_name)}
    assert project.columns("widget") == FINAL_WIDGET_COLUMNS
    assert project.widget_names() == ["seeded-1", "seeded-2"]


@pytest.mark.asyncio
async def test_a_range_in_the_middle_of_the_history(tmp_path, monkeypatch):
    project = SquashProject(tmp_path, monkeypatch)
    squashed_name = await project.squash("0004", start_name="0002", squashed_name="middle")
    assert squashed_name == "0002_middle"
    project.delete_migrations("0002_add_size", "0003_seed", "0004_color")
    await project.migrate()
    assert project.journal() == {("app", "0001_initial"), ("app", "0005_note"), ("app", "0002_middle")} | {
        ("app", name) for name in ("0002_add_size", "0003_seed", "0004_color")
    }
    assert project.columns("widget") == FINAL_WIDGET_COLUMNS


@pytest.mark.asyncio
async def test_squashing_a_squashed_migration_replaces_its_originals(tmp_path, monkeypatch):
    project = SquashProject(tmp_path, monkeypatch)
    first_squashed_name = await project.squash("0003")
    squashed = await squashmigrations(
        config=project.config, app_label="app", start_name=first_squashed_name, end_name="0005", squashed_name="all"
    )
    assert squashed.writer is not None
    assert squashed.writer.replaces == [("app", name) for name in MIGRATIONS]


@pytest.mark.asyncio
async def test_unapplying_to_zero_through_the_squashed_migration(tmp_path, monkeypatch):
    project = SquashProject(tmp_path, monkeypatch)
    squashed_name = await project.squash("0005")
    await project.migrate()
    await project.migrate(target="app.zero")
    assert project.journal() == set()
    assert "widget" not in project.tables()
    assert squashed_name.startswith("0001_")


@pytest.mark.asyncio
async def test_another_apps_dependency_on_a_replaced_migration(tmp_path, monkeypatch):
    project = SquashProject(tmp_path, monkeypatch, with_other_app=True)
    squashed_name = await project.squash("0005")
    project.delete_migrations(*MIGRATIONS)
    await project.migrate()
    assert ("other", "0001_initial") in project.journal()
    assert ("app", squashed_name) in project.journal()
    assert "gadget" in project.tables()


@pytest.mark.asyncio
async def test_deleted_replaced_files_after_every_database_applied_them(tmp_path, monkeypatch):
    project = SquashProject(tmp_path, monkeypatch)
    await project.migrate()
    squashed_name = await project.squash("0005")
    await project.migrate()
    project.delete_migrations(*MIGRATIONS)
    await project.migrate()
    assert project.journal() == ALL_APP_MIGRATIONS | {("app", squashed_name)}


@pytest.mark.asyncio
async def test_deleted_replaced_files_while_some_are_still_unapplied_is_refused(tmp_path, monkeypatch):
    project = SquashProject(tmp_path, monkeypatch)
    await project.migrate(target="app.0002_add_size")
    await project.squash("0005")
    project.delete_migrations("0003_seed", "0004_color", "0005_note")
    with pytest.raises(MigrationLoadError, match="only some are applied"):
        await project.migrate()


@pytest.mark.asyncio
async def test_squash_refuses_unknown_names_and_a_start_after_the_end(tmp_path, monkeypatch):
    project = SquashProject(tmp_path, monkeypatch)
    with pytest.raises(ConfigurationError, match="No migration"):
        await squashmigrations(config=project.config, app_label="app", end_name="9999")
    with pytest.raises(ConfigurationError, match="doesn't come before"):
        await squashmigrations(config=project.config, app_label="app", start_name="0004", end_name="0002")
    with pytest.raises(ConfigurationError, match="More than one"):
        await squashmigrations(config=project.config, app_label="app", end_name="000")
    single = await squashmigrations(config=project.config, app_label="app", start_name="0002", end_name="0002")
    assert single.writer is None


def test_elidable_is_written_into_the_migration_and_left_out_by_default():
    assert RunPython(RunPython.noop).elidable is False
    assert RunSQL("SELECT 1", elidable=True).deconstruct()[2]["elidable"] is True
    assert "elidable" not in RunSQL("SELECT 1").deconstruct()[2]
    assert RemoveField("Widget", "name").deconstruct()[2] == {"model_name": "Widget", "name": "name"}
