from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import pytest

from hare import Hare, fields
from hare.core.connections.connections import Connections
from hare.fields.field import Field
from hare.migrations.api import migrate
from hare.migrations.autodetection.migration_autodetector import MigrationAutodetector
from hare.migrations.operations import AddField, AlterField, CreateModel, RenameModel
from hare.migrations.state.state_apps import StateApps
from hare.models import Model

FROZEN_NOW = dt.datetime(2024, 1, 1, 12, 0)


def write_app_package(tmp_path: Path, app_label: str) -> str:
    package_dir = tmp_path / app_label
    migrations_dir = package_dir / "migrations"
    migrations_dir.mkdir(parents=True, exist_ok=True)
    (package_dir / "__init__.py").write_text("", encoding="ascii")
    (package_dir / "models.py").write_text("", encoding="ascii")
    (migrations_dir / "__init__.py").write_text("", encoding="ascii")
    return f"{app_label}.migrations"


def write_migration(
    tmp_path: Path, app_label: str, name: str, operations_source: list[str], dependencies: list[tuple[str, str]]
) -> None:
    content = [
        "from hare import migrations",
        "from hare.migrations import operations as ops",
        "from hare import fields",
        "",
        "class Migration(migrations.Migration):",
        f"    dependencies = {dependencies!r}",
        "",
        "    operations = [",
        *operations_source,
        "    ]",
        "",
    ]
    (tmp_path / app_label / "migrations" / f"{name}.py").write_text("\n".join(content), encoding="ascii")


def make_model(name: str, app_label: str, **model_fields: Field[Any]) -> type[Model]:
    attributes: dict[str, Any] = dict(model_fields)
    attributes["Meta"] = type("Meta", (), {"app": app_label, "table": name.lower()})
    return type(name, (Model,), attributes)


def build_config(tmp_path: Path, migration_modules: dict[str, str]) -> dict[str, Any]:
    return {
        "connections": {
            "default": {
                "engine": "sqlite+aiosqlite",
                "credentials": {"file_path": str(tmp_path / "db.sqlite3")},
            }
        },
        "apps": {
            app_label: {
                "models": [f"{app_label}.models"],
                "default_connection": "default",
                "migrations": migration_module,
            }
            for app_label, migration_module in migration_modules.items()
        },
    }


def build_autodetector(
    apps: StateApps, migration_modules: dict[str, str], target_app_labels: list[str] | None = None
) -> MigrationAutodetector:
    apps_config = {
        app_label: {"models": [], "default_connection": "default", "migrations": migration_module}
        for app_label, migration_module in migration_modules.items()
    }
    return MigrationAutodetector(apps, apps_config, now=lambda: FROZEN_NOW, target_app_labels=target_app_labels)


async def get_table_names(connection_alias: str = "default") -> set[str]:
    connection = Connections.get(connection_alias)
    _, rows = await connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    return {row["name"] for row in rows}


@pytest.fixture
def cross_app_rename_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Two apps already migrated: shop_app.Shop has a FK to owner_app.Owner."""
    owner_module = write_app_package(tmp_path, "owner_app")
    shop_module = write_app_package(tmp_path, "shop_app")
    write_migration(
        tmp_path,
        "owner_app",
        "0001_initial",
        [
            "        ops.CreateModel(",
            "            name='Owner',",
            "            fields=[('id', fields.IntField(generated=True, primary_key=True))],",
            "            options={'table': 'owner', 'app': 'owner_app', 'primary_key_attribute': 'id'},",
            "        ),",
        ],
        [],
    )
    write_migration(
        tmp_path,
        "shop_app",
        "0001_initial",
        [
            "        ops.CreateModel(",
            "            name='Shop',",
            "            fields=[",
            "                ('id', fields.IntField(generated=True, primary_key=True)),",
            "                ('owner', fields.ForeignKeyField('owner_app.Owner', source_field='owner_id', null=True,"
            " to_field='id', related_name='shops')),",
            "            ],",
            "            options={'table': 'shop', 'app': 'shop_app', 'primary_key_attribute': 'id'},",
            "        ),",
        ],
        [("owner_app", "0001_initial")],
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    return {"owner_app": owner_module, "shop_app": shop_module}


def build_renamed_owner_apps() -> StateApps:
    apps = StateApps()
    person = make_model("Person", "owner_app", id=fields.IntField(primary_key=True))
    shop = make_model(
        "Shop",
        "shop_app",
        id=fields.IntField(primary_key=True),
        owner=fields.ForeignKeyField("owner_app.Person", related_name="shops", null=True),
    )
    apps.register_model("owner_app", person)
    apps.register_model("shop_app", shop)
    return apps


@pytest.mark.asyncio
async def test_rename_model_depends_on_migrations_of_apps_still_referencing_the_old_name(
    cross_app_rename_project: dict[str, str],
) -> None:
    """A RenameModel migration used to depend only on its own app's previous migration, so the
    executor's topological order could place it BEFORE another app's already-written migration that
    still creates a FK to the old model name - that migration then crashed with "uninitialized
    relations". The rename must wait for every migration that references the old name, while the
    referencing app's own new AlterField (pointing at the new name) must wait for the rename."""
    autodetector = build_autodetector(build_renamed_owner_apps(), cross_app_rename_project)

    writers = {writer.app_label: writer for writer in await autodetector.changes()}

    owner_writer = writers["owner_app"]
    assert any(isinstance(operation, RenameModel) for operation in owner_writer.operations)
    assert ("shop_app", "0001_initial") in owner_writer.dependencies
    assert ("owner_app", "0001_initial") in owner_writer.dependencies

    shop_writer = writers["shop_app"]
    assert any(isinstance(operation, AlterField) for operation in shop_writer.operations)
    assert ("owner_app", owner_writer.name) in shop_writer.dependencies
    assert ("shop_app", "0001_initial") in shop_writer.dependencies


@pytest.mark.asyncio
async def test_cross_app_model_rename_migrates_from_scratch_and_leaves_no_further_changes(
    tmp_path: Path, cross_app_rename_project: dict[str, str]
) -> None:
    """End to end: the written rename migrations apply on an empty database (the plan places the
    rename after the referencing app's first migration), and afterwards a fresh autodetector pass
    finds nothing to do instead of crashing while replaying the migration history."""
    apps = build_renamed_owner_apps()
    for writer in await build_autodetector(apps, cross_app_rename_project).changes():
        writer.write()

    try:
        await migrate(config=build_config(tmp_path, cross_app_rename_project))

        assert {"person", "shop"} <= await get_table_names()
        assert "owner" not in await get_table_names()
        assert await build_autodetector(apps, cross_app_rename_project).changes() == []
    finally:
        await Hare.close_connections()


@pytest.mark.asyncio
async def test_same_app_model_rename_with_referencing_model_gets_no_extra_dependencies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rename whose only referencing model lives in the SAME app needs no cross-app dependency."""
    module = write_app_package(tmp_path, "single_app")
    write_migration(
        tmp_path,
        "single_app",
        "0001_initial",
        [
            "        ops.CreateModel(",
            "            name='Owner',",
            "            fields=[('id', fields.IntField(generated=True, primary_key=True))],",
            "            options={'table': 'owner', 'app': 'single_app', 'primary_key_attribute': 'id'},",
            "        ),",
            "        ops.CreateModel(",
            "            name='Shop',",
            "            fields=[",
            "                ('id', fields.IntField(generated=True, primary_key=True)),",
            "                ('owner', fields.ForeignKeyField('single_app.Owner', source_field='owner_id', null=True,"
            " to_field='id', related_name='shops')),",
            "            ],",
            "            options={'table': 'shop', 'app': 'single_app', 'primary_key_attribute': 'id'},",
            "        ),",
        ],
        [],
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    apps = StateApps()
    apps.register_model("single_app", make_model("Person", "single_app", id=fields.IntField(primary_key=True)))
    apps.register_model(
        "single_app",
        make_model(
            "Shop",
            "single_app",
            id=fields.IntField(primary_key=True),
            owner=fields.ForeignKeyField("single_app.Person", related_name="shops", null=True),
        ),
    )

    writers = await build_autodetector(apps, {"single_app": module}).changes()

    assert len(writers) == 1
    assert writers[0].dependencies == [("single_app", "0001_initial")]
    assert any(isinstance(operation, RenameModel) for operation in writers[0].operations)
    assert not any(isinstance(operation, (CreateModel, AddField)) for operation in writers[0].operations)


def build_cyclic_fk_apps() -> StateApps:
    """Two brand-new apps, migrated for the first time together, with a genuine mutual FK cycle
    across the app boundary: cycle_shop_app.Shop.owner -> cycle_owner_app.Owner and
    cycle_owner_app.Owner.favorite -> cycle_shop_app.Shop."""
    apps = StateApps()
    shop = make_model(
        "Shop",
        "cycle_shop_app",
        id=fields.IntField(primary_key=True),
        owner=fields.ForeignKeyField("cycle_owner_app.Owner", related_name="shops", null=True),
    )
    owner = make_model(
        "Owner",
        "cycle_owner_app",
        id=fields.IntField(primary_key=True),
        favorite=fields.ForeignKeyField("cycle_shop_app.Shop", related_name="fans", null=True),
    )
    apps.register_model("cycle_shop_app", shop)
    apps.register_model("cycle_owner_app", owner)
    return apps


@pytest.fixture
def cyclic_fk_migration_modules(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    shop_module = write_app_package(tmp_path, "cycle_shop_app")
    owner_module = write_app_package(tmp_path, "cycle_owner_app")
    monkeypatch.syspath_prepend(str(tmp_path))
    return {"cycle_shop_app": shop_module, "cycle_owner_app": owner_module}


@pytest.mark.asyncio
async def test_first_time_cross_app_fk_cycle_splits_into_a_follow_up_migration(
    cyclic_fk_migration_modules: dict[str, str],
) -> None:
    """makemigrations used to write BOTH apps' initial migrations with the cyclic FK inline in
    CreateModel and a dependency edge in each direction (see _relation_dependencies), an
    unsatisfiable 2-node cycle in the migration graph itself - migrate()/downgrade() then always
    failed with "Circular dependency detected", with no warning at generation time. One of the
    two relation fields must instead be deferred into a follow-up AddField migration, exactly
    like OperationGenerator already does for a same-app model cycle, so the whole batch forms a
    valid acyclic graph."""
    writers = await build_autodetector(build_cyclic_fk_apps(), cyclic_fk_migration_modules).changes()

    assert len(writers) == 3
    writers_by_app: dict[str, list[Any]] = {}
    for writer in writers:
        writers_by_app.setdefault(writer.app_label, []).append(writer)
    assert {app_label: len(entries) for app_label, entries in writers_by_app.items()} in (
        {"cycle_shop_app": 2, "cycle_owner_app": 1},
        {"cycle_shop_app": 1, "cycle_owner_app": 2},
    )

    initial_writers = {writer.app_label: writer for writer in writers if writer.initial}
    follow_up_writer = next(writer for writer in writers if not writer.initial)
    assert set(initial_writers) == {"cycle_shop_app", "cycle_owner_app"}

    # Neither initial CreateModel may still carry the field that closes the cycle - it was
    # deferred into the follow-up migration below.
    for writer in initial_writers.values():
        create_model = next(op for op in writer.operations if isinstance(op, CreateModel))
        field_names = {name for name, _field in create_model.fields}
        assert {"owner", "favorite"} & field_names != {"owner", "favorite"}

    add_field_ops = [op for op in follow_up_writer.operations if isinstance(op, AddField)]
    assert len(add_field_ops) == 1
    assert add_field_ops[0].name in ("owner", "favorite")

    # The follow-up migration must depend on BOTH apps' initial migrations - the field it adds
    # references a model in the OTHER app, which must already exist.
    assert (follow_up_writer.app_label, initial_writers[follow_up_writer.app_label].name) in (
        follow_up_writer.dependencies
    )
    other_app_label = next(app_label for app_label in initial_writers if app_label != follow_up_writer.app_label)
    assert (other_app_label, initial_writers[other_app_label].name) in follow_up_writer.dependencies

    # The app whose field was deferred has no cross-app dependency left in its initial
    # migration - it only gets one, via the follow-up migration, once that field is added back.
    # The OTHER app's initial migration still legitimately depends on it (its own, undeferred
    # field still references the deferring app's model) - that single remaining edge is what the
    # follow-up migration's own dependency on both apps closes without forming a cycle.
    deferring_app_label = follow_up_writer.app_label
    assert initial_writers[deferring_app_label].dependencies == []


@pytest.mark.asyncio
async def test_cross_app_fk_cycle_migrates_from_scratch_and_leaves_no_further_changes(
    tmp_path: Path, cyclic_fk_migration_modules: dict[str, str]
) -> None:
    """End to end: the split migrations apply cleanly on an empty database in graph order, and a
    fresh autodetector pass afterwards finds nothing left to do."""
    apps = build_cyclic_fk_apps()
    for writer in await build_autodetector(apps, cyclic_fk_migration_modules).changes():
        writer.write()

    try:
        await migrate(config=build_config(tmp_path, cyclic_fk_migration_modules))

        assert {"shop", "owner"} <= await get_table_names()
        assert await build_autodetector(apps, cyclic_fk_migration_modules).changes() == []
    finally:
        await Hare.close_connections()
