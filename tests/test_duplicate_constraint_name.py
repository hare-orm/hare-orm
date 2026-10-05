"""Regression coverage for an explicitly-named UniqueConstraint/ExclusionConstraint/Index
declared on an abstract base colliding across every concrete subclass - see
Apps.init_relations() in hare/core/apps.py, right after the table-name collision check it mirrors.

Uses dynamically-registered, throwaway model modules (injected into sys.modules), following the
same pattern as test_duplicate_table_name.py - locally-defined classes never become real module
attributes, so the module-scanning "modules=[...]" config can't discover them otherwise.
"""

from __future__ import annotations

import sys
import types

import pytest

from hare import fields
from hare.core.config import HareConfig
from hare.core.hare_context import HareContext
from hare.ddl.constraints import UniqueConstraint
from hare.ddl.indexes import Index
from hare.exceptions import ConfigurationError
from hare.models import Model

MODULE_NAME = "tests._duplicate_constraint_models"


def _register_module(**models: type[Model]) -> None:
    module = types.ModuleType(MODULE_NAME)
    for name, model in models.items():
        setattr(module, name, model)
    sys.modules[MODULE_NAME] = module


@pytest.mark.asyncio
async def test_abstract_base_unique_constraint_name_collision_raises_clear_error():
    """An explicit UniqueConstraint name= on an abstract base is inherited unchanged by every
    concrete subclass - a real constraint is backed by a database index, and index names occupy
    one namespace per (connection, schema), not per table. Used to only surface once schema
    creation hit the DB with a baffling raw "index already exists" error - caught up front
    instead, naming both colliding models."""

    class Base(Model):
        id = fields.IntField(primary_key=True)
        team_id = fields.IntField()

        class Meta:
            abstract = True
            app = "models"
            constraints = (UniqueConstraint(fields=("team_id",), name="uq_open_team"),)

    class ChildA(Base):
        class Meta:
            app = "models"
            table = "child_a"

    class ChildB(Base):
        class Meta:
            app = "models"
            table = "child_b"

    _register_module(ChildA=ChildA, ChildB=ChildB)
    try:
        with pytest.raises(ConfigurationError) as exc_info:
            async with HareContext() as ctx:
                await ctx.init(HareConfig.from_db_url("sqlite+aiosqlite://:memory:", {"models": [MODULE_NAME]}))
        message = str(exc_info.value)
        assert "ChildA" in message
        assert "ChildB" in message
        assert "uq_open_team" in message
    finally:
        sys.modules.pop(MODULE_NAME, None)


@pytest.mark.asyncio
async def test_abstract_base_index_name_collision_raises_clear_error():
    class Base(Model):
        id = fields.IntField(primary_key=True)
        team_id = fields.IntField()

        class Meta:
            abstract = True
            app = "models"
            indexes = (Index(fields=("team_id",), name="idx_open_team"),)

    class ChildA(Base):
        class Meta:
            app = "models"
            table = "child_a"

    class ChildB(Base):
        class Meta:
            app = "models"
            table = "child_b"

    _register_module(ChildA=ChildA, ChildB=ChildB)
    try:
        with pytest.raises(ConfigurationError) as exc_info:
            async with HareContext() as ctx:
                await ctx.init(HareConfig.from_db_url("sqlite+aiosqlite://:memory:", {"models": [MODULE_NAME]}))
        message = str(exc_info.value)
        assert "ChildA" in message
        assert "ChildB" in message
        assert "idx_open_team" in message
    finally:
        sys.modules.pop(MODULE_NAME, None)


@pytest.mark.asyncio
async def test_abstract_base_unnamed_unique_constraint_does_not_collide():
    """An unnamed UniqueConstraint's generated name is auto-hashed and embeds the table name
    (unlike an explicit name=) - inheriting it from an abstract base must not raise, since each
    subclass's own constraint name is already distinct."""

    class Base(Model):
        id = fields.IntField(primary_key=True)
        team_id = fields.IntField()

        class Meta:
            abstract = True
            app = "models"
            constraints = (UniqueConstraint(fields=("team_id",)),)

    class ChildA(Base):
        class Meta:
            app = "models"
            table = "child_a"

    class ChildB(Base):
        class Meta:
            app = "models"
            table = "child_b"

    _register_module(ChildA=ChildA, ChildB=ChildB)
    try:
        async with HareContext() as ctx:
            await ctx.init(HareConfig.from_db_url("sqlite+aiosqlite://:memory:", {"models": [MODULE_NAME]}))
            await ctx.generate_schemas()
    finally:
        sys.modules.pop(MODULE_NAME, None)


@pytest.mark.asyncio
async def test_two_unnamed_unique_constraints_on_the_same_fields_raise_clear_error():
    """Two unnamed UniqueConstraints on EXACTLY the same fields (same order) generate the identical
    auto-derived constraint name - the generated name is a pure function of (table, field names).
    Confirmed live on Postgres: the second one fails schema creation with a raw "relation ...
    already exists" straight from the database. Caught up front here instead, before any DDL
    ever runs."""

    class StockEntry(Model):
        id = fields.IntField(primary_key=True)
        sku = fields.CharField(max_length=20)
        warehouse = fields.CharField(max_length=20)

        class Meta:
            app = "models"
            constraints = (
                UniqueConstraint(fields=("sku", "warehouse")),
                UniqueConstraint(fields=("sku", "warehouse")),
            )

    _register_module(StockEntry=StockEntry)
    try:
        with pytest.raises(ConfigurationError, match="two unnamed"):
            async with HareContext() as ctx:
                await ctx.init(HareConfig.from_db_url("sqlite+aiosqlite://:memory:", {"models": [MODULE_NAME]}))
    finally:
        sys.modules.pop(MODULE_NAME, None)


@pytest.mark.asyncio
async def test_explicitly_named_unique_constraint_duplicating_an_unnamed_one_is_not_a_collision():
    """The opposite of the test above - an EXPLICITLY named UniqueConstraint on the same fields as
    an unnamed one is a deliberate, distinct-by-name second constraint (redundant in effect, but
    not a name collision), so it must not raise."""

    class StockEntry(Model):
        id = fields.IntField(primary_key=True)
        sku = fields.CharField(max_length=20)
        warehouse = fields.CharField(max_length=20)

        class Meta:
            app = "models"
            constraints = (
                UniqueConstraint(fields=("sku", "warehouse")),
                UniqueConstraint(fields=("sku", "warehouse"), name="uq_stock_entry_explicit"),
            )

    _register_module(StockEntry=StockEntry)
    try:
        async with HareContext() as ctx:
            await ctx.init(HareConfig.from_db_url("sqlite+aiosqlite://:memory:", {"models": [MODULE_NAME]}))
            await ctx.generate_schemas()
    finally:
        sys.modules.pop(MODULE_NAME, None)


@pytest.mark.asyncio
async def test_unique_index_duplicating_unique_constraint_raises_clear_error():
    """A unique Index(fields=...) in Meta.indexes and a UniqueConstraint(fields=...) in
    Meta.constraints on the exact same fields never collide by name (Index auto-derives its name
    with the "uidx"/"idx" prefix, UniqueConstraint with "uid"), so generate_schemas() wouldn't
    fail - it would silently create two separate physical unique indexes enforcing the identical
    rule. Caught up front here instead, before any redundant DDL is ever generated."""

    class AuditEntry(Model):
        id = fields.IntField(primary_key=True)
        a = fields.IntField()
        b = fields.IntField()

        class Meta:
            app = "models"
            constraints = (UniqueConstraint(fields=("a", "b"), name="uq_audit_a_b"),)
            indexes = (Index(fields=("a", "b"), name="idx_audit_a_b", unique=True),)

    _register_module(AuditEntry=AuditEntry)
    try:
        with pytest.raises(ConfigurationError, match="Meta.indexes"):
            async with HareContext() as ctx:
                await ctx.init(HareConfig.from_db_url("sqlite+aiosqlite://:memory:", {"models": [MODULE_NAME]}))
    finally:
        sys.modules.pop(MODULE_NAME, None)


@pytest.mark.asyncio
async def test_unique_index_duplicating_unique_constraint_field_order_still_raises():
    """Field order doesn't matter for this collision - a UNIQUE on (a, b) enforces the same rule
    as one on (b, a), unlike the unique_together-vs-constraints name-collision check above where
    order is part of what makes the generated names collide."""

    class AuditEntry(Model):
        id = fields.IntField(primary_key=True)
        a = fields.IntField()
        b = fields.IntField()

        class Meta:
            app = "models"
            constraints = (UniqueConstraint(fields=("a", "b")),)
            indexes = (Index(fields=("b", "a"), unique=True),)

    _register_module(AuditEntry=AuditEntry)
    try:
        with pytest.raises(ConfigurationError, match="Meta.indexes"):
            async with HareContext() as ctx:
                await ctx.init(HareConfig.from_db_url("sqlite+aiosqlite://:memory:", {"models": [MODULE_NAME]}))
    finally:
        sys.modules.pop(MODULE_NAME, None)


@pytest.mark.asyncio
async def test_unique_index_and_unique_constraint_on_different_fields_is_not_a_collision():
    """A unique Index and a UniqueConstraint that cover genuinely different field sets are two
    unrelated uniqueness rules, not a redundant duplicate - must not raise."""

    class AuditEntry(Model):
        id = fields.IntField(primary_key=True)
        a = fields.IntField()
        b = fields.IntField()
        c = fields.IntField()

        class Meta:
            app = "models"
            constraints = (UniqueConstraint(fields=("a", "b")),)
            indexes = (Index(fields=("a", "c"), unique=True),)

    _register_module(AuditEntry=AuditEntry)
    try:
        async with HareContext() as ctx:
            await ctx.init(HareConfig.from_db_url("sqlite+aiosqlite://:memory:", {"models": [MODULE_NAME]}))
            await ctx.generate_schemas()
    finally:
        sys.modules.pop(MODULE_NAME, None)


@pytest.mark.asyncio
async def test_explicit_constraint_name_on_different_connections_is_not_a_collision():
    """Mirrors test_duplicate_table_name.py's identical carve-out for table names - two models on
    genuinely different connections never share a physical index namespace."""

    class WidgetA(Model):
        id = fields.IntField(primary_key=True)
        team_id = fields.IntField()

        class Meta:
            app = "modelsa"
            table = "widget_a"
            constraints = (UniqueConstraint(fields=("team_id",), name="uq_shared_name"),)

    class WidgetB(Model):
        id = fields.IntField(primary_key=True)
        team_id = fields.IntField()

        class Meta:
            app = "modelsb"
            table = "widget_b"
            constraints = (UniqueConstraint(fields=("team_id",), name="uq_shared_name"),)

    module_a_name = "tests._duplicate_constraint_appa_models"
    module_b_name = "tests._duplicate_constraint_appb_models"
    module_a = types.ModuleType(module_a_name)
    setattr(module_a, "WidgetA", WidgetA)  # noqa: B010
    sys.modules[module_a_name] = module_a
    module_b = types.ModuleType(module_b_name)
    setattr(module_b, "WidgetB", WidgetB)  # noqa: B010
    sys.modules[module_b_name] = module_b

    try:
        async with HareContext() as ctx:
            await ctx.init(
                config={
                    "connections": {
                        "default": "sqlite+aiosqlite://:memory:",
                        "secondary": "sqlite+aiosqlite://:memory:",
                    },
                    "apps": {
                        "modelsa": {"models": [module_a_name], "default_connection": "default"},
                        "modelsb": {"models": [module_b_name], "default_connection": "secondary"},
                    },
                },
            )
            await ctx.generate_schemas()
    finally:
        sys.modules.pop(module_a_name, None)
        sys.modules.pop(module_b_name, None)
