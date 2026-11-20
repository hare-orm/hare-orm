"""Tests for hare.cli.shell_launcher.ShellLauncher.build_namespace()."""

import sys
import types

import pytest

from hare import Hare, Model, fields
from hare.cli.shell_launcher import ShellLauncher
from hare.core.hare_context import HareContext

pytestmark = pytest.mark.database_independent


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

    assert namespace["apps"].get_binding(namespace["Item"]).app in ("shop", "warehouse")
    assert namespace["apps"].get_binding(namespace["shop_Item"]).app == "shop"
    assert namespace["apps"].get_binding(namespace["warehouse_Item"]).app == "warehouse"

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

    assert namespace["apps"].get_binding(namespace["Item"]).app == "lonely"
    assert namespace["lonely_Item"] is namespace["Item"]
    captured = capsys.readouterr()
    assert captured.out == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("composite", [False, True], ids=["scalar", "composite"])
async def test_shell_names_preserve_services_and_unambiguous_qualified_models(monkeypatch, capsys, reverse, composite):
    cases = (
        (("shop", "Item"), ("warehouse", "shop_Item")),
        (("shop", "Warehouse_Item"), ("shop_Warehouse", "Item")),
        (("shop", "Hare"), ("shop", "hare"), ("shop", "apps")),
    )
    for case_number, declarations in enumerate(cases):
        modules = {}
        expected = {}
        for app_label, model_name in reversed(declarations) if reverse else declarations:
            module_name = f"scratch_shell_names_{case_number}_{app_label}"
            if app_label not in modules:
                modules[app_label] = types.ModuleType(module_name)
                monkeypatch.setitem(sys.modules, module_name, modules[app_label])
            attributes = {
                "__module__": module_name,
                "number": fields.IntField(primary_key=not composite, generated=False),
                "Meta": type("Meta", (), {"app": app_label, "table": f"shell_{case_number}_{len(expected)}"}),
            }
            if composite:
                attributes.update(region=fields.IntField(), pk=fields.CompositePrimaryKey("region", "number"))
            model = type(model_name, (Model,), attributes)
            setattr(modules[app_label], model_name, model)
            expected[app_label, model_name] = model
        async with HareContext() as context:
            await context.init(
                config={
                    "connections": {"default": "sqlite+aiosqlite://:memory:"},
                    "apps": {label: {"models": [module]} for label, module in modules.items()},
                },
                connect=False,
            )
            namespace = ShellLauncher.build_namespace(context)
            assert namespace["Hare"] is Hare
            assert namespace["hare"] is context
            assert namespace["apps"] is context.apps
            for (app_label, model_name), model in expected.items():
                assert namespace["apps"][app_label][model_name] is model
                qualified_name = f"{app_label}_{model_name}"
                if case_number == 1:
                    assert qualified_name not in namespace
                else:
                    assert namespace[qualified_name] is model
        output = capsys.readouterr().out
        assert "Warning:" in output
        if case_number == 1:
            assert "alias is omitted" in output
