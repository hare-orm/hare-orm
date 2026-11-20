"""Regression coverage for get_or_create() reading its existence check on the write alias, like
Django - a read replica may not have the row yet, and a miss there would create a duplicate.

Uses a dynamically-registered, throwaway model module (injected into sys.modules) rather than
tests/shared_models/testmodels.py, following the same pattern as test_m2m_cross_connection.py.
"""

from __future__ import annotations

import asyncio
import os
import sys
import types

import pytest
import pytest_asyncio

from hare import fields
from hare.contrib.test import requires_features
from hare.models import Model
from hare.query.expressions import Q
from hare.transactions.transactions import Transactions
from tests.utils.multi_database_context import MultiDatabaseTestContext

MODULE_NAME = "tests._get_or_create_router_models"


class ReadWriteSplitRouter:
    def db_for_read(self, model):
        return "read_replica"

    def db_for_write(self, model):
        return "write_primary"


@requires_features(supports_select_for_update=True)
@pytest.mark.asyncio
async def test_locking_reads_use_the_write_router_for_every_result_shape(read_write_split):
    _, model = read_write_split

    async def seed():
        await model.objects.using("write_primary").create(id=1, name="primary")
        await model.objects.using("read_replica").create(id=1, name="replica")

    # A write in the reader's task would pin its ordinary reads to the primary and hide
    # a wrong routing decision by select_for_update().
    await asyncio.create_task(seed())
    assert (await model.objects.get(id=1)).name == "replica"
    async with Transactions.atomic("write_primary"):
        for repeat in range(2):
            for shape in ("get", "get_q", "first", "list", "values", "values_list"):
                query = model.objects.select_for_update()
                if shape == "get":
                    result = await query.get(id=1)
                elif shape == "get_q":
                    result = await query.get(Q(id=1))
                elif shape == "first":
                    result = await query.filter(id=1).first()
                elif shape == "list":
                    result = await query.filter(id=1)
                elif shape == "values":
                    result = await query.values("name").get(id=1)
                else:
                    result = await query.values_list("name", flat=True).get(id=1)
                if shape == "values":
                    assert result == {"name": "primary"}, (repeat, shape)
                elif shape == "values_list":
                    assert result == "primary", (repeat, shape)
                else:
                    objs = result if shape == "list" else [result]
                    assert [obj.name for obj in objs] == ["primary"], (repeat, shape)


@pytest_asyncio.fixture
async def read_write_split():
    class RoutedWidget(Model):
        id = fields.IntField(primary_key=True)
        name = fields.TextField()

        class Meta:
            app = "get_or_create_router"

    module = types.ModuleType(MODULE_NAME)
    setattr(module, "RoutedWidget", RoutedWidget)  # noqa: B010
    sys.modules[MODULE_NAME] = module

    db_url = os.getenv("HARE_TEST_DB", "sqlite+aiosqlite://:memory:")
    try:
        async with MultiDatabaseTestContext.open(
            db_url,
            ["write_primary", "read_replica"],
            apps={"get_or_create_router": {"models": [MODULE_NAME], "default_connection": "write_primary"}},
            routers=[ReadWriteSplitRouter],
        ) as ctx:
            await ctx.generate_schemas()
            # Schema generation only ever creates tables for a model's static default
            # connection ("write_primary" here) - "read_replica" needs the same DDL applied
            # manually to simulate an already-replicated alias, same as what a real router setup
            # assumes is already in place.
            write_connection = ctx.connections.get("write_primary")
            read_db = ctx.connections.get("read_replica")
            await read_db.execute_script(write_connection.get_schema_sql(safe=True))
            yield ctx, RoutedWidget
    finally:
        sys.modules.pop(MODULE_NAME, None)


@pytest.mark.asyncio
async def test_get_or_create_finds_row_not_yet_on_the_read_alias(read_write_split):
    """A row on the write alias that the read replica doesn't have yet (replication lag) is found -
    the existence check reads on the write alias, like Django, instead of missing the row on
    db_for_read's alias and creating a duplicate."""
    ctx, RoutedWidget = read_write_split
    read_db = ctx.connections.get("read_replica")
    write_connection = ctx.connections.get("write_primary")

    pre_existing = await RoutedWidget.objects.create(name="PreExisting")

    fetched, created = await RoutedWidget.objects.get_or_create(name="PreExisting")
    assert created is False
    assert fetched.id == pre_existing.id

    _, write_rows = await write_connection.execute('SELECT * FROM "routedwidget"')
    _, read_rows = await read_db.execute('SELECT * FROM "routedwidget"')
    assert len(write_rows) == 1
    assert len(read_rows) == 0


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_get_or_create_finds_row_created_in_the_open_transaction(read_write_split):
    """A row created earlier in a still-open transaction on the write alias is found, not
    duplicated - the read replica can't see it."""
    ctx, RoutedWidget = read_write_split
    write_connection = ctx.connections.get("write_primary")

    async with Transactions.atomic("write_primary"):
        pre_existing = await RoutedWidget.objects.create(name="InTransaction")
        fetched, created = await RoutedWidget.objects.get_or_create(name="InTransaction")
        assert created is False
        assert fetched.id == pre_existing.id

    _, write_rows = await write_connection.execute('SELECT * FROM "routedwidget"')
    assert len(write_rows) == 1


@pytest.mark.asyncio
async def test_get_or_create_creates_on_the_write_alias_when_missing_everywhere(read_write_split):
    """The create-fallback path must still land on db_for_write, not db_for_read."""
    ctx, RoutedWidget = read_write_split
    write_connection = ctx.connections.get("write_primary")
    read_db = ctx.connections.get("read_replica")

    fetched, created = await RoutedWidget.objects.get_or_create(name="BrandNew")
    assert created is True

    _, write_rows = await write_connection.execute('SELECT * FROM "routedwidget"')
    _, read_rows = await read_db.execute('SELECT * FROM "routedwidget"')
    assert len(write_rows) == 1
    assert len(read_rows) == 0
