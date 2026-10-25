"""A rollback walks one state back through the migrations - rendered once and taken back migration by
migration - so unapplying K migrations costs what applying them does."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from hare import Hare
from hare.core.connections.connections import Connections
from hare.migrations.api import migrate
from hare.migrations.execution.executor.plan_step import PlanStep
from hare.migrations.execution.executor.rollback_walk import RollbackWalk
from hare.migrations.loading.graph.migration_key import MigrationKey
from hare.migrations.migration import Migration
from hare.migrations.state.model_state import ModelState

pytestmark = pytest.mark.sqlite_only

CHAIN_LENGTH = 40

MIGRATION_TEMPLATE = """from hare import migrations
from hare import fields
from hare.migrations import operations as ops


class Migration(migrations.Migration):
    dependencies = {dependencies}

    operations = [
{operations}
    ]
"""


def get_create_model(app_label: str, number: int) -> str:
    relation = (
        f"                ('previous', fields.ForeignKeyField('{app_label}.Model{number - 1}', "
        "related_name='next_models')),\n"
        if number
        else ""
    )
    return (
        "        ops.CreateModel(\n"
        f"            name='Model{number}',\n"
        "            fields=[\n"
        "                ('id', fields.IntField(primary_key=True)),\n"
        "                ('label', fields.CharField(max_length=20, default='')),\n"
        f"{relation}"
        "            ],\n"
        f"            options={{'table': 'model{number}'}},\n"
        "        ),"
    )


def get_field_changes(number: int) -> str:
    return (
        f"        ops.AddField(model_name='Model{number}', name='rank', field=fields.IntField(default=0)),\n"
        f"        ops.AlterField(model_name='Model{number}', name='rank', field=fields.BigIntField(default=0)),\n"
        f"        ops.RenameField(model_name='Model{number}', old_name='label', new_name='title'),\n"
        f"        ops.RemoveField(model_name='Model{number}', name='rank'),"
    )


def write_chain(tmp_path: Path, app_label: str, operations_by_migration: list[str]) -> str:
    package_dir = tmp_path / app_label
    migrations_dir = package_dir / "migrations"
    migrations_dir.mkdir(parents=True)
    (package_dir / "__init__.py").write_text("", encoding="ascii")
    (package_dir / "models.py").write_text("", encoding="ascii")
    (migrations_dir / "__init__.py").write_text("", encoding="ascii")
    previous_name = None
    for number, operations in enumerate(operations_by_migration):
        name = f"{number + 1:04d}_step"
        dependencies = [(app_label, previous_name)] if previous_name else []
        (migrations_dir / f"{name}.py").write_text(
            MIGRATION_TEMPLATE.format(dependencies=dependencies, operations=operations), encoding="ascii"
        )
        previous_name = name
    return f"{app_label}.migrations"


def get_config(tmp_path: Path, app_label: str, database_name: str) -> dict[str, object]:
    return {
        "connections": {
            "default": {
                "engine": "sqlite+aiosqlite",
                "credentials": {"file_path": str(tmp_path / database_name)},
            }
        },
        "apps": {
            app_label: {
                "models": [f"{app_label}.models"],
                "default_connection": "default",
                "migrations": f"{app_label}.migrations",
            }
        },
    }


async def get_schema() -> list[tuple[str, str]]:
    _, rows = await Connections.get("default").execute(
        "SELECT name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' AND name != 'hare_migrations' "
        "ORDER BY name"
    )
    # SQLite keeps a table's statement as it was sent - a table rebuilt is spaced otherwise.
    return [(row["name"], re.sub(r"\s+", " ", row["sql"]).replace("( ", "(").replace(" )", ")")) for row in rows]


@pytest.fixture
def render_count(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    rendered_names: list[str] = []
    render = ModelState.render

    def counting_render(model_state: ModelState, *args: object, **kwargs: object) -> object:
        rendered_names.append(model_state.name)
        return render(model_state, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(ModelState, "render", counting_render)
    return rendered_names


@pytest.mark.asyncio
async def test_a_rollback_of_a_chain_renders_each_model_a_bounded_number_of_times(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, render_count: list[str]
) -> None:
    write_chain(tmp_path, "long_chain", [get_create_model("long_chain", number) for number in range(CHAIN_LENGTH)])
    monkeypatch.syspath_prepend(str(tmp_path))
    config = get_config(tmp_path, "long_chain", "chain.sqlite3")
    try:
        await migrate(config=config)
        assert len(await get_schema()) == CHAIN_LENGTH
        render_count.clear()
        await migrate(config=config, target="long_chain.0001_step")
        assert [name for name, _sql in await get_schema()] == ["model0"]
    finally:
        await Hare.close_connections()
    # Projecting the state renders no model; unapplying renders each one once, then takes it out in place.
    assert len(render_count) <= 3 * CHAIN_LENGTH


@pytest.mark.asyncio
async def test_a_rollback_part_way_leaves_the_schema_a_migrate_to_that_point_makes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    operations_by_migration = []
    for number in range(8):
        operations_by_migration.append(get_create_model("changed_chain", number))
        operations_by_migration.append(get_field_changes(number))
    operations_by_migration.append(
        "        ops.RenameModel(old_name='Model3', new_name='Renamed3'),\n        ops.DeleteModel(name='Model7'),"
    )
    write_chain(tmp_path, "changed_chain", operations_by_migration)
    monkeypatch.syspath_prepend(str(tmp_path))
    middle = f"changed_chain.{7:04d}_step"
    try:
        await migrate(config=get_config(tmp_path, "changed_chain", "direct.sqlite3"), target=middle)
        expected_schema = await get_schema()
    finally:
        await Hare.close_connections()
    config = get_config(tmp_path, "changed_chain", "rolled_back.sqlite3")
    try:
        await migrate(config=config)
        await migrate(config=config, target=middle)
        assert await get_schema() == expected_schema
        await migrate(config=config, target="changed_chain.zero")
        assert await get_schema() == []
    finally:
        await Hare.close_connections()


def test_a_plan_applying_a_migration_or_out_of_order_is_not_walked() -> None:
    keys = [MigrationKey(app_label="app", name=f"{number:04d}") for number in range(3)]
    migrations = {key: Migration(key.name, key.app_label) for key in keys}

    def get_plan(*steps: tuple[int, bool]) -> list[PlanStep]:
        return [PlanStep(migrations[keys[position]], backward) for position, backward in steps]

    assert RollbackWalk.fits(get_plan((2, True), (1, True)), keys)
    assert not RollbackWalk.fits(get_plan((1, True), (2, True)), keys)
    assert not RollbackWalk.fits(get_plan((2, True), (2, False)), keys)
    assert not RollbackWalk.fits([PlanStep(Migration("0009", "app"), True)], keys)
