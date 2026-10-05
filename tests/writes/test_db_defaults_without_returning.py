"""A database without INSERT ... RETURNING (``Features.supports_returning`` False): the INSERT asks
for nothing back, the new key comes the dialect's own way (SQLite's last row id), and the values the
database gave the ``db_default`` columns stay unread until ``refresh_from_db()``, as in Django."""

from __future__ import annotations

import sys
import types

import pytest
import pytest_asyncio

from hare import fields
from hare.core.hare_context import HareContext
from hare.fields.composite_primary_key import CompositePrimaryKey
from hare.fields.database_default import DatabaseDefault
from hare.models import Model

pytestmark = pytest.mark.sqlite_only

MODULE_NAME = "tests._db_defaults_without_returning_models"


class Counter(Model):
    id = fields.IntField(primary_key=True)
    number = fields.IntField(db_default=7)
    label = fields.CharField(max_length=20, db_default="fresh")

    class Meta:
        app = "without_returning"


class PairCounter(Model):
    left = fields.IntField()
    right = fields.IntField()
    number = fields.IntField(db_default=7)
    pk = CompositePrimaryKey("left", "right")

    class Meta:
        app = "without_returning"


@pytest_asyncio.fixture
async def without_returning():
    module = types.ModuleType(MODULE_NAME)
    for model in (Counter, PairCounter):
        setattr(module, model.__name__, model)
    sys.modules[MODULE_NAME] = module
    try:
        async with HareContext() as context:
            await context.init(
                config={
                    "connections": {"default": "sqlite+aiosqlite://:memory:"},
                    "apps": {"without_returning": {"models": [MODULE_NAME], "default_connection": "default"}},
                }
            )
            connection = context.connections.get("default")
            connection.features = connection.features.replace(supports_returning=False)
            await context.generate_schemas()
            yield connection
    finally:
        sys.modules.pop(MODULE_NAME, None)


@pytest.mark.asyncio
async def test_database_defaults_are_read_by_refresh_from_db(without_returning):
    statements = []
    original_execute_query = without_returning.execute

    async def recording_execute_query(query, values=None, **kwargs):
        statements.append(query)
        return await original_execute_query(query, values, **kwargs)

    without_returning.execute = recording_execute_query
    counter = await Counter.objects.create()
    assert counter.id is not None
    assert isinstance(counter.number, DatabaseDefault)
    assert isinstance(counter.label, DatabaseDefault)
    assert not any("RETURNING" in statement for statement in statements)
    await counter.refresh_from_db()
    assert (counter.number, counter.label) == (7, "fresh")

    partly_given = await Counter.objects.create(label="given")
    assert isinstance(partly_given.number, DatabaseDefault)
    assert partly_given.label == "given"
    assert partly_given.id == counter.id + 1


@pytest.mark.asyncio
async def test_given_primary_key(without_returning):
    counter = await Counter.objects.create(id=40)
    assert counter.id == 40
    await counter.refresh_from_db()
    assert (counter.number, counter.label) == (7, "fresh")


@pytest.mark.asyncio
async def test_composite_primary_key(without_returning):
    pair = await PairCounter.objects.create(left=1, right=2)
    assert pair.pk == (1, 2)
    await pair.refresh_from_db()
    assert pair.number == 7
