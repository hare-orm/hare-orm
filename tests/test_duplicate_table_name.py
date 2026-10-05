"""Regression coverage for two unrelated model classes silently colliding on the same default
table name - see Apps.init_relations() in hare/core/apps.py, right after db_table is assigned.

Uses dynamically-registered, throwaway model modules (injected into sys.modules), following the
same pattern as test_m2m_cross_connection.py.
"""

from __future__ import annotations

import sys
import types

import pytest

from hare import fields
from hare.core.hare_context import HareContext
from hare.exceptions import ConfigurationError
from hare.models import Model

MODULE_A = "tests._duplicate_table_appa2_models"
MODULE_B = "tests._duplicate_table_appb2_models"


@pytest.mark.asyncio
async def test_same_class_name_in_different_apps_raises_clear_configuration_error():
    """Two unrelated Widget classes in different apps, no FK between them, both defaulting to
    db_table "widget" - used to only surface once schema generation's topological sort choked on
    it (a baffling "cyclic fk references" ConfigurationError with no mention of the real cause).
    The collision must be caught up front instead, naming both classes and the shared table."""

    class Widget(Model):
        id = fields.IntField(primary_key=True)
        name = fields.TextField()

        class Meta:
            app = "appa2"

    module_a = types.ModuleType(MODULE_A)
    setattr(module_a, "Widget", Widget)  # noqa: B010
    sys.modules[MODULE_A] = module_a

    class OtherWidget(Model):
        id = fields.IntField(primary_key=True)
        label = fields.TextField()

        class Meta:
            app = "appb2"

    # Renamed AFTER class creation so both classes genuinely share __name__ "Widget" (the actual
    # scenario: two independently-authored apps that happen to name a model the same thing) -
    # a Python class body can't declare two classes under one name in the same module anyway.
    OtherWidget.__name__ = "Widget"
    OtherWidget.__qualname__ = "Widget"
    module_b = types.ModuleType(MODULE_B)
    setattr(module_b, "Widget", OtherWidget)  # noqa: B010
    sys.modules[MODULE_B] = module_b

    ctx = HareContext()
    try:
        with pytest.raises(ConfigurationError) as exc_info:
            async with ctx:
                await ctx.init(
                    config={
                        "connections": {"default": "sqlite+aiosqlite://:memory:"},
                        "apps": {
                            "appa2": {"models": [MODULE_A], "default_connection": "default"},
                            "appb2": {"models": [MODULE_B], "default_connection": "default"},
                        },
                    },
                )
        message = str(exc_info.value)
        assert "cyclic" not in message.lower()
        assert "appa2.Widget" in message
        assert "appb2.Widget" in message
        assert '"widget"' in message
    finally:
        sys.modules.pop(MODULE_A, None)
        sys.modules.pop(MODULE_B, None)


@pytest.mark.asyncio
async def test_same_table_name_on_different_connections_is_not_a_collision():
    """Two models sharing a default table name but pinned to DIFFERENT connections are two
    genuinely separate physical tables - not a collision, must not raise."""

    class Widget(Model):
        id = fields.IntField(primary_key=True)
        name = fields.TextField()

        class Meta:
            app = "appa2"

    module_a = types.ModuleType(MODULE_A)
    setattr(module_a, "Widget", Widget)  # noqa: B010
    sys.modules[MODULE_A] = module_a

    class OtherWidget(Model):
        id = fields.IntField(primary_key=True)
        label = fields.TextField()

        class Meta:
            app = "appb2"

    OtherWidget.__name__ = "Widget"
    OtherWidget.__qualname__ = "Widget"
    module_b = types.ModuleType(MODULE_B)
    setattr(module_b, "Widget", OtherWidget)  # noqa: B010
    sys.modules[MODULE_B] = module_b

    ctx = HareContext()
    try:
        async with ctx:
            await ctx.init(
                config={
                    "connections": {
                        "default": "sqlite+aiosqlite://:memory:",
                        "secondary": "sqlite+aiosqlite://:memory:",
                    },
                    "apps": {
                        "appa2": {"models": [MODULE_A], "default_connection": "default"},
                        "appb2": {"models": [MODULE_B], "default_connection": "secondary"},
                    },
                },
            )
            await ctx.generate_schemas()
    finally:
        sys.modules.pop(MODULE_A, None)
        sys.modules.pop(MODULE_B, None)
