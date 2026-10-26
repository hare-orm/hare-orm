"""The CHECK of an IntEnumField and a CharEnumField in migrations - where the column holds any integer or text:
makemigrations writes it with the model, replaces it when the members change, and the database then refuses a
value of no member; the schema shows no drift."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest

from hare.exceptions import IntegrityError
from hare.migrations.drift.detect_drift_for_alias import detect_drift_for_alias
from tests.migrations.state.test_swappable_models import (
    CONNECTION,
    build_config,
    get_connection_config,
    make_migrations,
    migrate,
    open_context,
)

SHOP_SOURCE = """
from enum import IntEnum, StrEnum

from hare import fields
from hare.models import Model


class Status(StrEnum):
    NEW = "new"
    PAID = "paid"
MORE_STATUSES


class Level(IntEnum):
    LOW = 1
    HIGH = 2


class Order(Model):
    id = fields.IntField(primary_key=True, generated=False)
    status = fields.CharEnumField(Status, max_length=10)
    level = fields.IntEnumField(Level, null=True)

    class Meta:
        table = "enum_check_order"
"""


class ShopProject:
    """The shop app written to disk - a fresh package for every version, with the migrations written so far
    copied in."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.migration_files: list[Path] = []
        self.package = ""

    def write_version(self, more_statuses: str) -> dict[str, dict[str, Any]]:
        """Writes the app anew - ``Status`` with ``more_statuses`` added.

        Returns:
            The apps config.
        """
        self.package = f"enum_check_shop_{uuid.uuid4().hex[:10]}"
        package_dir = self.root / self.package
        (package_dir / "migrations").mkdir(parents=True)
        (package_dir / "__init__.py").write_text("", encoding="utf-8")
        (package_dir / "models.py").write_text(SHOP_SOURCE.replace("MORE_STATUSES", more_statuses), encoding="utf-8")
        (package_dir / "migrations" / "__init__.py").write_text("", encoding="utf-8")
        for migration_file in self.migration_files:
            (package_dir / "migrations" / migration_file.name).write_bytes(migration_file.read_bytes())
        return {
            "shop": {
                "models": [f"{self.package}.models"],
                "default_connection": CONNECTION,
                "migrations": f"{self.package}.migrations",
            }
        }


@pytest.fixture
def shop_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ShopProject:
    monkeypatch.syspath_prepend(str(tmp_path))
    return ShopProject(tmp_path)


async def write_migrations(
    context: Any, apps_config: dict[str, dict[str, Any]], shop_project: ShopProject
) -> list[str]:
    """Writes the migrations of the changes, kept for the next versions - their texts."""
    written = await make_migrations(context, apps_config)
    shop_project.migration_files.extend(written)
    return [path.read_text(encoding="utf-8") for path in written]


@pytest.mark.asyncio
async def test_an_enum_check_follows_the_members_through_migrations(shop_project: ShopProject, tmp_path: Path):
    connection_config = get_connection_config(tmp_path)
    if connection_config["engine"].startswith("clickhouse"):
        pytest.skip("an enum column of ClickHouse holds only the members")
    apps_config = shop_project.write_version("")
    async with open_context(build_config(connection_config, apps_config), create_db=True) as context:
        [migration_text] = await write_migrations(context, apps_config, shop_project)
        assert 'EnumMembersCondition(field="status", values=("new", "paid"))' in migration_text
        assert 'EnumMembersCondition(field="level", values=(1, 2))' in migration_text
        await migrate(context, apps_config)
        order_model = context.apps.get_model("shop.Order")
        connection = context.connections.get(CONNECTION)
        await order_model.objects.create(id=1, status="paid", level=None)
        for assignment in ("\"status\" = 'sent'", '"level" = 3'):
            with pytest.raises(IntegrityError):
                await connection.execute_script(f'UPDATE "enum_check_order" SET {assignment}')
        assert not (await detect_drift_for_alias(context.apps, apps_config, CONNECTION)).has_drift

    apps_config = shop_project.write_version('    SENT = "sent"')
    async with open_context(build_config(connection_config, apps_config)) as context:
        [migration_text] = await write_migrations(context, apps_config, shop_project)
        assert "RemoveConstraint" in migration_text and "AddConstraint" in migration_text
        assert 'values=("new", "paid", "sent")' in migration_text
        await migrate(context, apps_config)
        connection = context.connections.get(CONNECTION)
        await connection.execute_script('UPDATE "enum_check_order" SET "status" = \'sent\'')
        order_model = context.apps.get_model("shop.Order")
        assert await order_model.objects.values_list("status", flat=True) == ["sent"]
        assert not (await detect_drift_for_alias(context.apps, apps_config, CONNECTION)).has_drift
