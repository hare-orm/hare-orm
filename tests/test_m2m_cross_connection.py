"""Regression coverage for ManyToManyRelation.add()/remove()/clear() defaulting to the wrong
connection in a multi-connection (cross-app) setup - see relational.py's _through_table_db().

Uses two throwaway, dynamically-registered model modules (injected into sys.modules) instead of
tests/testmodels.py - that module is shared by nearly the whole suite under a single-app config,
and a genuine cross-app model reference there breaks collection for everything else that loads it.
"""

from __future__ import annotations

import os
import sys
import types

import pytest
import pytest_asyncio

from hare import fields, prefetch_related_objects
from hare.models import Model
from tests.utils.multi_database_context import MultiDatabaseTestContext

OWNER_MODULE_NAME = "tests._cross_app_m2m_owner_models"
TARGET_MODULE_NAME = "tests._cross_app_m2m_target_models"
ROUTED_MODULE_NAME = "tests._routed_m2m_models"


@pytest_asyncio.fixture
async def cross_app_m2m():
    class CrossAppM2MTarget(Model):
        id = fields.IntField(primary_key=True)
        name = fields.TextField()

        owners: fields.ManyToManyRelation["CrossAppM2MOwner"]

        class Meta:
            app = "cam2m_target"

    class CrossAppM2MOwner(Model):
        id = fields.IntField(primary_key=True)
        name = fields.TextField()
        # db_constraint=False isolates this from the separate, known cross-database
        # FK-constraint limitation - only the connection-resolution bug is under test here.
        targets: fields.ManyToManyRelation[CrossAppM2MTarget] = fields.ManyToManyField(
            "cam2m_target.CrossAppM2MTarget", db_constraint=False
        )

        class Meta:
            app = "cam2m_owner"

    owner_module = types.ModuleType(OWNER_MODULE_NAME)
    setattr(owner_module, "CrossAppM2MOwner", CrossAppM2MOwner)  # noqa: B010
    target_module = types.ModuleType(TARGET_MODULE_NAME)
    setattr(target_module, "CrossAppM2MTarget", CrossAppM2MTarget)  # noqa: B010
    sys.modules[OWNER_MODULE_NAME] = owner_module
    sys.modules[TARGET_MODULE_NAME] = target_module

    db_url = os.getenv("HARE_TEST_DB", "sqlite://:memory:")
    try:
        async with MultiDatabaseTestContext.open(
            db_url,
            ["cam2m_owner", "cam2m_target"],
            apps={
                "cam2m_owner": {"models": [OWNER_MODULE_NAME], "default_connection": "cam2m_owner"},
                "cam2m_target": {"models": [TARGET_MODULE_NAME], "default_connection": "cam2m_target"},
            },
        ) as ctx:
            await ctx.generate_schemas()
            yield ctx, CrossAppM2MOwner, CrossAppM2MTarget
    finally:
        sys.modules.pop(OWNER_MODULE_NAME, None)
        sys.modules.pop(TARGET_MODULE_NAME, None)


@pytest.mark.asyncio
async def test_m2m_add_remove_clear_use_the_owning_side_connection(cross_app_m2m):
    """ManyToManyRelation.add()/remove()/clear() used to default to
    self.remote_model._meta.db (the RELATED model's own connection) when no using= was
    passed - but the through table is only ever created once, on the model that owns the
    forward-declared (non-generated) M2M field (_get_m2m_tables() skips the auto-generated
    backward field entirely). CrossAppM2MOwner declares the forward field targeting
    CrossAppM2MTarget in a different connection - calling add()/remove()/clear() with no
    using= used to connect to the target's DB and fail with "no such table", since the
    through table only exists in the owner's DB."""
    ctx, CrossAppM2MOwner, CrossAppM2MTarget = cross_app_m2m
    owner = await CrossAppM2MOwner.objects.create(name="Owner")
    target = await CrossAppM2MTarget.objects.create(name="Target")

    owner_db = ctx.connections.get("cam2m_owner")
    through_table = CrossAppM2MOwner._meta.fields_map["targets"].through

    async def linked() -> bool:
        _, rows = await owner_db.execute(f'SELECT 1 FROM "{through_table}"')  # noqa: S608
        return bool(rows)

    await owner.targets.add(target)
    assert await linked()

    await owner.targets.remove(target)
    assert not await linked()

    await owner.targets.add(target)
    await owner.targets.clear()
    assert not await linked()


@pytest_asyncio.fixture
async def routed_m2m():
    """A single app/connection ("default") plus a second alias ("routed") that a configured
    ConnectionRouter sends every model to - through the model's own get_connection(), not the static
    per-app default_connection. The through table is only ever real on "routed", so add()/
    remove()/clear() still targeting "default" (the pre-fix bug) hits an empty table there."""

    class RoutedEvent(Model):
        id = fields.IntField(primary_key=True)
        name = fields.TextField()
        participants: fields.ManyToManyRelation["RoutedTeam"] = fields.ManyToManyField(
            "routed_m2m.RoutedTeam", related_name="events", db_constraint=False
        )

        class Meta:
            app = "routed_m2m"

    class RoutedTeam(Model):
        id = fields.IntField(primary_key=True)
        name = fields.TextField()

        events: fields.ManyToManyRelation[RoutedEvent]

        class Meta:
            app = "routed_m2m"

    class EverythingToRoutedRouter:
        def db_for_read(self, model):
            return "routed"

        def db_for_write(self, model):
            return "routed"

    module = types.ModuleType(ROUTED_MODULE_NAME)
    setattr(module, "RoutedEvent", RoutedEvent)  # noqa: B010
    setattr(module, "RoutedTeam", RoutedTeam)  # noqa: B010
    sys.modules[ROUTED_MODULE_NAME] = module

    db_url = os.getenv("HARE_TEST_DB", "sqlite://:memory:")
    try:
        async with MultiDatabaseTestContext.open(
            db_url,
            ["default", "routed"],
            apps={"routed_m2m": {"models": [ROUTED_MODULE_NAME], "default_connection": "default"}},
            routers=[EverythingToRoutedRouter],
        ) as ctx:
            await ctx.generate_schemas()
            # Schema generation only ever creates tables for a model's static default
            # connection ("default" here) - "routed" needs the same DDL applied manually to
            # simulate an already-migrated alias, same as the real-world setup a router assumes.
            default_db = ctx.connections.get("default")
            routed_db = ctx.connections.get("routed")
            await routed_db.execute_script(default_db.get_schema_sql(safe=True))
            yield ctx, RoutedEvent, RoutedTeam
    finally:
        sys.modules.pop(ROUTED_MODULE_NAME, None)


@pytest.mark.asyncio
async def test_m2m_add_remove_clear_use_the_configured_router_connection(routed_m2m):
    """ManyToManyRelation.add()/remove()/clear() used to read the through table's connection off
    the static `_meta.db` (Connections.get(default_connection)), never consulting a configured
    ConnectionRouter - every other write path on the model already goes through get_connection(),
    which does. With a router sending every model to "routed", add()/remove()/clear() must land
    there too, not silently on "default"."""
    ctx, RoutedEvent, RoutedTeam = routed_m2m
    event = await RoutedEvent.objects.create(name="Launch")
    team = await RoutedTeam.objects.create(name="Rockets")

    routed_db = ctx.connections.get("routed")
    default_db = ctx.connections.get("default")
    through_table = RoutedEvent._meta.fields_map["participants"].through

    async def routed_row_count() -> int:
        _, rows = await routed_db.execute(f'SELECT 1 FROM "{through_table}"')  # noqa: S608
        return len(rows)

    async def default_row_count() -> int:
        _, rows = await default_db.execute(f'SELECT 1 FROM "{through_table}"')  # noqa: S608
        return len(rows)

    await event.participants.add(team)
    assert await routed_row_count() == 1
    assert await default_row_count() == 0

    await prefetch_related_objects([event], "participants")
    assert event.participants.related_objects == [team]

    await event.participants.remove(team)
    assert await routed_row_count() == 0

    await event.participants.add(team)
    await event.participants.clear()
    assert await routed_row_count() == 0
