"""Bulk inserts bind every batch before execution and retain their input order."""

from datetime import UTC, datetime

import pytest

from hare import fields
from hare.contrib.test import hare_test_context
from hare.exceptions import QueryError
from hare.instrumentation.declarations import QueryExecuted
from hare.instrumentation.observers.observers import Observers
from hare.models import Model
from hare.models.write.instance_writer import InstanceWriter
from hare.query.statements.write.bulk.create.prepared_bulk_insert import PreparedBulkInsert

pytestmark = pytest.mark.sqlite_only

default_calls = []


async def deferred_amount():
    default_calls.append("computed")
    return 19


class PlainEntry(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)
    amount = fields.IntField(db_default=7)


class DeferredEntry(Model):
    id = fields.IntField(primary_key=True)
    amount = fields.IntField(default=deferred_amount)


class Entry(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50, unique=True)
    amount = fields.IntField()
    modified = fields.DatetimeField(auto_now=True)


@pytest.mark.asyncio
async def test_skipped_conflict_restores_automatic_stamp():
    async with hare_test_context([__name__], db_url="sqlite+aiosqlite://:memory:"):
        await Entry.objects.create(name="conflict", amount=1)
        previous = datetime(2000, 1, 1, tzinfo=UTC)
        skipped = Entry(id=2, name="conflict", amount=2, modified=previous)
        inserted = Entry(id=3, name="inserted", amount=3, modified=previous)
        result = await Entry.objects.bulk_create(
            [skipped, inserted], ignore_conflicts=True, on_conflict=["name"], returning=True
        )
        assert result == [inserted]
        assert skipped.modified == previous
        assert not skipped._saved_in_db
        assert inserted.modified != previous
        assert inserted._saved_in_db


@pytest.mark.parametrize("returning", [False, True])
@pytest.mark.asyncio
async def test_invalid_later_batch_is_rejected_before_any_insert(returning):
    async with hare_test_context([__name__], db_url="sqlite+aiosqlite://:memory:"):
        objects = [Entry(id=1, name="first", amount=1), Entry(id=2, name="second", amount=2)]
        before = [obj.modified for obj in objects]
        objects[1].amount = "invalid"
        events = []
        with Observers.observing(QueryExecuted, events.append):
            with pytest.raises((TypeError, ValueError)):
                await Entry.objects.bulk_create(objects, batch_size=1, returning=returning)
            await Observers.wait_for_pending()
        assert not [event for event in events if event.sql.lstrip().upper().startswith("INSERT")]
        assert [obj.modified for obj in objects] == before
        assert all(not obj._saved_in_db for obj in objects)
        assert not await Entry.objects.exists()


@pytest.mark.parametrize("returning", [False, True])
@pytest.mark.asyncio
async def test_execution_uses_the_bound_values_when_source_objects_change(returning):
    async with hare_test_context([__name__], db_url="sqlite+aiosqlite://:memory:"):
        moment = datetime(2026, 1, 1, tzinfo=UTC)
        objects = [
            Entry(id=1, name="first", amount=10, modified=moment),
            Entry(id=2, name="second", amount=20, modified=moment),
        ]
        query = Entry.objects.bulk_create(objects, batch_size=1, returning=returning)._get_execution_query(True)
        query._writer = InstanceWriter(Entry, query._connection)
        prepared = PreparedBulkInsert(query)
        assert prepared.bound.requires_transaction
        objects[0].amount = 99
        await prepared.execute(query)
        assert await Entry.objects.order_by("id").values_list("amount", flat=True) == [10, 20]


@pytest.mark.parametrize("batch_size", [None, 1, 2])
@pytest.mark.asyncio
async def test_repeated_upsert_keys_keep_the_last_input_value(batch_size):
    async with hare_test_context([__name__], db_url="sqlite+aiosqlite://:memory:"):
        objects = [
            Entry(id=1, name="same", amount=1),
            Entry(id=2, name="other", amount=2),
            Entry(id=3, name="same", amount=3),
        ]
        await Entry.objects.bulk_create(
            objects, batch_size=batch_size, returning=True, update_fields=["amount"], on_conflict=["name"]
        )
        assert await Entry.objects.order_by("name").values_list("amount", flat=True) == [2, 3]


@pytest.mark.parametrize("returning", [False, True])
@pytest.mark.asyncio
async def test_preview_uses_the_same_batches_for_mixed_database_defaults(returning):
    async with hare_test_context([__name__], db_url="sqlite+aiosqlite://:memory:"):
        objects = [
            PlainEntry(id=1, name="O'Brien; ? $2", amount=10),
            PlainEntry(id=2, name="default"),
            PlainEntry(id=3, name="last", amount=12),
        ]
        query = PlainEntry.objects.bulk_create(objects, returning=returning, batch_size=2)
        preview = query.preview()
        assert preview.requires_transaction is True
        assert preview.deferred_fields == ()
        assert len(preview.commands) == 3
        statements = query._get_execution_query(True)._get_statements(False)
        assert [sql for sql, _values in statements] == [command.sql for command in preview.commands]
        inline = query._get_execution_query(True)._get_statements(True)
        assert len(inline) == 3
        assert "O''Brien; ? $2" in inline[0][0]
        for sql, values in inline:
            assert not values
            await PlainEntry.get_connection().execute(sql, values, returns_rows=returning)
        assert await PlainEntry.objects.order_by("id").values_list("amount", flat=True) == [10, 7, 12]


@pytest.mark.asyncio
async def test_preview_defers_async_defaults_without_calling_or_changing_them():
    default_calls.clear()
    async with hare_test_context([__name__], db_url="sqlite+aiosqlite://:memory:"):
        obj = DeferredEntry()
        before = dict(vars(obj))
        query = DeferredEntry.objects.bulk_create([obj])
        preview = query.preview()
        assert preview.commands is None
        assert preview.requires_transaction is None
        assert preview.deferred_fields == ("amount",)
        with pytest.raises(QueryError, match="deferred"):
            query.sql()
        assert default_calls == []
        assert vars(obj) == before
        await query
        assert default_calls == ["computed"]
        assert (await DeferredEntry.objects.get()).amount == 19


@pytest.mark.asyncio
async def test_preview_reports_automatic_stamps_and_distinguishes_empty_writes():
    async with hare_test_context([__name__], db_url="sqlite+aiosqlite://:memory:"):
        obj = Entry(name="stamp", amount=1)
        before = dict(vars(obj))
        assert Entry.objects.bulk_create([obj]).preview().deferred_fields == ("modified",)
        assert vars(obj) == before
        preview = Entry.objects.bulk_create([]).preview()
        assert preview.commands == ()
        assert preview.requires_transaction is False
