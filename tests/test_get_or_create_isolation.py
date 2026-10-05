"""The create step of get_or_create() / update_or_create(): a plain INSERT in autocommit, a SAVEPOINT
inside an open transaction, a transaction for a model overriding save() - and in every case a
concurrent writer's row (IntegrityError) is recovered from."""

from __future__ import annotations

import os
import sys
import types

import pytest
import pytest_asyncio

from hare import fields
from hare.contrib.test import requires_features
from hare.instrumentation.declarations import TransactionEvent
from hare.instrumentation.observers.observers import Observers
from hare.models import Model
from hare.query.statements.write.create_or_update import CreateOrUpdate
from hare.transactions.transactions import Transactions
from tests.utils.multi_database_context import MultiDatabaseTestContext

MODULE_NAME = "tests._get_or_create_isolation_models"


class IsolatedTag(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=20, unique=True)
    value = fields.IntField(default=0)

    class Meta:
        app = "get_or_create_isolation"


class IsolatedAudit(Model):
    id = fields.IntField(primary_key=True)
    text = fields.CharField(max_length=40)

    class Meta:
        app = "get_or_create_isolation"


class AuditedTag(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=20, unique=True)

    class Meta:
        app = "get_or_create_isolation"

    async def save(self, *args, **kwargs):
        await IsolatedAudit.objects.using(kwargs.get("using")).create(text=f"saving {self.name}")
        await super().save(*args, **kwargs)


@pytest_asyncio.fixture
async def isolation_context():
    module = types.ModuleType(MODULE_NAME)
    for model in (IsolatedTag, IsolatedAudit, AuditedTag):
        setattr(module, model.__name__, model)
    sys.modules[MODULE_NAME] = module
    db_url = os.getenv("HARE_TEST_DB", "sqlite+aiosqlite://:memory:")
    try:
        async with MultiDatabaseTestContext.open(
            db_url,
            ["default"],
            apps={"get_or_create_isolation": {"models": [MODULE_NAME], "default_connection": "default"}},
        ) as ctx:
            await ctx.generate_schemas()
            yield ctx
    finally:
        sys.modules.pop(MODULE_NAME, None)


@pytest.mark.asyncio
async def test_create_outside_a_transaction_opens_none(isolation_context):
    events = []
    with Observers.observing(TransactionEvent, events.append):
        tag, created = await IsolatedTag.objects.get_or_create(name="new")
    assert created is True
    assert events == []
    _, created = await IsolatedTag.objects.update_or_create(name="other", defaults={"value": 1})
    assert created is True
    assert await IsolatedTag.objects.all().count() == 2


@requires_features(supports_unique_constraints=True)
@pytest.mark.asyncio
async def test_concurrent_row_is_recovered_outside_a_transaction(isolation_context):
    existing = await IsolatedTag.objects.create(name="raced")
    db = IsolatedTag.get_connection(for_write=True)
    # The existence check missed the row a concurrent writer created - the INSERT fails.
    queryset = IsolatedTag.objects.using(db)
    instance, created = await CreateOrUpdate.create_or_get(queryset, {}, {"name": "raced"})
    assert (instance.id, created) == (existing.id, False)
    await CreateOrUpdate.update_with_defaults(queryset, instance, {"value": 5}, db)
    assert (await IsolatedTag.objects.get(id=existing.id)).value == 5
    assert await IsolatedTag.objects.all().count() == 1


@requires_features(supports_transactions=True)
@requires_features(supports_unique_constraints=True)
@pytest.mark.asyncio
async def test_concurrent_row_is_recovered_inside_a_transaction(isolation_context):
    existing = await IsolatedTag.objects.create(name="raced")
    async with Transactions.atomic("default") as connection:
        instance, created = await CreateOrUpdate.create_or_get(
            IsolatedTag.objects.using(connection), {}, {"name": "raced"}
        )
        assert (instance.id, created) == (existing.id, False)
        # The failed INSERT rolled back to its savepoint only - the transaction goes on.
        await IsolatedTag.objects.using(connection).create(name="after")
    assert sorted(await IsolatedTag.objects.all().values_list("name", flat=True)) == ["after", "raced"]


@requires_features(supports_transactions=True)
@requires_features(supports_unique_constraints=True)
@pytest.mark.asyncio
async def test_overridden_save_is_rolled_back_with_a_failed_insert(isolation_context):
    existing = await AuditedTag.objects.create(name="raced")
    db = AuditedTag.get_connection(for_write=True)
    instance, created = await CreateOrUpdate.create_or_get(AuditedTag.objects.using(db), {}, {"name": "raced"})
    assert (instance.id, created) == (existing.id, False)
    # Only the audit row of the first, successful create is left.
    assert await IsolatedAudit.objects.all().values_list("text", flat=True) == ["saving raced"]
