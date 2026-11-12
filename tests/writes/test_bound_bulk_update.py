"""Bulk UPDATE binds rows once and renders structural columns without SQL rewriting."""

from datetime import UTC, datetime

import pytest

from hare import fields
from hare.contrib.test import hare_test_context
from hare.ddl.raw_sql_term import RawSQLTerm
from hare.dialects.dialect_registry import DialectRegistry
from hare.exceptions import QueryError
from hare.models import Model
from hare.query.statements.write.bulk.declarations import BulkUpdateRun
from hare.sql.builder.tables.table import Table
from hare.sql.builder.update_from_values import UpdateFromValues, ValuesColumn
from hare.sql.terms.values.literal_value import LiteralValue
from hare.time import Timezone

pytestmark = pytest.mark.sqlite_only


class PlainEntry(Model):
    id = fields.IntField(primary_key=True)
    amount = fields.IntField()


class StampedEntry(Model):
    id = fields.IntField(primary_key=True)
    amount = fields.IntField()
    modified = fields.DatetimeField(auto_now=True)


class ContactEntry(Model):
    key = fields.EmailField(primary_key=True)
    amount = fields.IntField()
    modified = fields.DatetimeField(auto_now=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["Ann@EXAMPLE.COM", "Bob@example.com"])
async def test_bulk_update_matches_returned_normalized_keys_before_finalizing_stamps(key):
    async with hare_test_context([__name__], db_url="sqlite+aiosqlite://:memory:"):
        obj = await ContactEntry.objects.create(key=key, amount=1)
        obj.key = key
        obj.amount = 2
        await ContactEntry.objects.bulk_update([obj], fields=["amount"])
        stored = await ContactEntry.objects.get(pk=key)
        assert stored.amount == obj.amount == 2
        assert stored.modified == obj.modified


@pytest.mark.database_independent
@pytest.mark.parametrize("dialect_name", ["sqlite", "postgresql", "clickhouse"])
def test_update_rows_keep_identifiers_and_raw_sql_structural(dialect_name):
    dialect = DialectRegistry.get_dialect(dialect_name)
    context = dialect.sql_context
    table = Table('target.with,"quote')
    literal = f"{context.quote(table.get_table_name())}.payload"
    raw = f"length('{literal}') > 0"
    write = UpdateFromValues(
        table=table,
        assignments=(('amount,.quoted"', ValuesColumn(2)), ("version", table["version"] + LiteralValue("1"))),
        matches=(('key,.one"', ValuesColumn(0)), ("version", ValuesColumn(1))),
        columns=("c0", "c1", "c2"),
        alias="source.with,comma",
        condition=RawSQLTerm(raw),
        returned=(),
    )
    sql = dialect.clauses.get_update_from_values_sql(
        write=write, with_sql="", values_sql="(1, 2, 3)", sql_context=context
    )
    assert raw in sql
    assert context.quote('amount,.quoted"') in sql
    if dialect_name == "clickhouse":
        assert "row.1 = " + context.quote('key,.one"') in sql
        assert "tupleElement(arrayElement(" in sql
        assert context.quote(table.get_table_name()) + "." + context.quote("version") not in sql


@pytest.mark.asyncio
async def test_bound_update_does_not_reread_changed_input_values():
    async with hare_test_context([__name__], db_url="sqlite+aiosqlite://:memory:"):
        objects = [await PlainEntry.objects.create(amount=index) for index in range(2)]
        for obj in objects:
            obj.amount += 10
        query = PlainEntry.objects.bulk_update(objects, fields=["amount"], batch_size=1)
        preview = query.preview()
        assert len(preview.commands) == 2
        assert preview.requires_transaction is False
        execution = query._get_execution_query(True)
        run = BulkUpdateRun(execution._connection, list(objects))
        batches = execution._make_queries(run)
        for obj in objects:
            obj.amount = 999
        await execution._execute_statements(run, run.connection, batches)
        assert await PlainEntry.objects.order_by("id").values_list("amount", flat=True) == [10, 11]


@pytest.mark.asyncio
async def test_deferred_preview_never_stamps_and_failed_explicit_batch_restores_only_unwritten(monkeypatch):
    async with hare_test_context([__name__], db_url="sqlite+aiosqlite://:memory:"):
        objects = [await StampedEntry.objects.create(amount=index) for index in range(2)]
        previous = [obj.modified for obj in objects]
        for obj in objects:
            obj.amount += 10
        query = StampedEntry.objects.bulk_update(objects, fields=["amount"], batch_size=1)
        calls = []
        moment = datetime(2040, 1, 1, tzinfo=UTC)

        def clock():
            calls.append("clock")
            return moment

        monkeypatch.setattr(Timezone, "now", clock)
        monkeypatch.setattr(fields.DatetimeField, "get_stamp_value", lambda self, now=None: clock())
        preview = query.preview()
        assert preview.commands is None
        assert preview.deferred_fields == ("modified",)
        with pytest.raises(QueryError, match="deferred"):
            query.sql()
        assert calls == []

        connection = StampedEntry.get_connection()
        execute = connection.execute
        statements = []

        async def fail_second(sql, *args, **kwargs):
            if sql.startswith("UPDATE"):
                statements.append(sql)
                if len(statements) == 2:
                    raise RuntimeError("second batch failed")
            return await execute(sql, *args, **kwargs)

        monkeypatch.setattr(connection, "execute", fail_second)
        with pytest.raises(RuntimeError, match="second batch failed"):
            await query
        assert calls == ["clock"]
        assert objects[0].modified == moment
        assert objects[1].modified == previous[1]
        assert await StampedEntry.objects.order_by("id").values_list("amount", flat=True) == [10, 1]
        empty = StampedEntry.objects.bulk_update([], fields=["amount"]).preview()
        assert empty.commands == ()
        assert empty.requires_transaction is False
