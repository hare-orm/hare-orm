"""Tests for hare.cli.shell_launcher.ShellLauncher.build_namespace()."""

import sys
import types

import pytest

from hare.cli.shell_launcher import ShellLauncher
from hare.core.hare_context import HareContext


def _register_module(name: str, source: str) -> None:
    module = types.ModuleType(name)
    exec(compile(source, name, "exec"), module.__dict__)
    sys.modules[name] = module


TWO_APPS_SAME_MODEL_NAME_SOURCE = """
from hare import fields
from hare.models import Model


class Item(Model):
    id = fields.IntField(primary_key=True)

    class Meta:
        app = {app_label!r}
        table = {table!r}
"""


@pytest.mark.asyncio
async def test_build_namespace_collision_keeps_both_models_reachable(capsys: pytest.CaptureFixture[str]) -> None:
    """Two apps registering a model class under the SAME name used to silently overwrite the
    first app's class in the shell namespace with no warning at all - a user typing "Item" got
    whichever app happened to load last, with zero indication the other app's model wasn't
    reachable by that name at all. Both must stay reachable (one under the plain name, both
    under an app-qualified name), with a warning printed about the ambiguity."""
    _register_module(
        "scratch_shell_test_shop.models",
        TWO_APPS_SAME_MODEL_NAME_SOURCE.format(app_label="shop", table="shop_item"),
    )
    _register_module(
        "scratch_shell_test_warehouse.models",
        TWO_APPS_SAME_MODEL_NAME_SOURCE.format(app_label="warehouse", table="warehouse_item"),
    )

    ctx = HareContext()
    async with ctx:
        await ctx.init(
            config={
                "connections": {"default": "sqlite+aiosqlite://:memory:"},
                "apps": {
                    "shop": {"models": ["scratch_shell_test_shop.models"], "default_connection": "default"},
                    "warehouse": {
                        "models": ["scratch_shell_test_warehouse.models"],
                        "default_connection": "default",
                    },
                },
            },
            _create_db=True,
        )
        namespace = ShellLauncher.build_namespace(ctx)

    assert namespace["Item"]._meta.app in ("shop", "warehouse")
    assert namespace["shop_Item"]._meta.app == "shop"
    assert namespace["warehouse_Item"]._meta.app == "warehouse"

    captured = capsys.readouterr()
    assert "Item" in captured.out
    assert "multiple apps" in captured.out


@pytest.mark.asyncio
async def test_build_namespace_no_collision_no_warning(capsys: pytest.CaptureFixture[str]) -> None:
    _register_module(
        "scratch_shell_test_lonely.models",
        TWO_APPS_SAME_MODEL_NAME_SOURCE.format(app_label="lonely", table="lonely_item"),
    )

    ctx = HareContext()
    async with ctx:
        await ctx.init(
            config={
                "connections": {"default": "sqlite+aiosqlite://:memory:"},
                "apps": {"lonely": {"models": ["scratch_shell_test_lonely.models"], "default_connection": "default"}},
            },
            _create_db=True,
        )
        namespace = ShellLauncher.build_namespace(ctx)

    assert namespace["Item"]._meta.app == "lonely"
    assert namespace["lonely_Item"] is namespace["Item"]
    captured = capsys.readouterr()
    assert captured.out == ""
