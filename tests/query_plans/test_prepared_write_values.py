"""Write compilation paths share converted assignments and automatic timestamps."""

from copy import copy
from datetime import UTC, datetime, timedelta

import pytest

from hare import fields
from hare.contrib.test import hare_test_context
from hare.models import Model

pytestmark = pytest.mark.sqlite_only

conversions = []
stamps = []


class ConvertedText(fields.CharField):
    def to_db_value(self, value, instance):
        conversions.append(value)
        return super().to_db_value(f"{value}:{len(conversions)}", instance)


class StampedTime(fields.DatetimeField):
    @staticmethod
    def get_auto_now_value(now=None):
        stamps.append("stamp")
        return datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=len(stamps))


class Entry(Model):
    id = fields.IntField(primary_key=True)
    value = ConvertedText(max_length=80)
    modified = StampedTime(auto_now=True)


@pytest.mark.asyncio
async def test_repeated_update_prepares_fresh_values_once_per_execution():
    async with hare_test_context([__name__], db_url="sqlite+aiosqlite://:memory:"):
        await Entry.objects.create(id=1, value="initial")
        conversions.clear()
        stamps.clear()
        query = Entry.objects.filter(id=1).update(value="next")
        for iteration in range(1, 4):
            await query
            assert conversions == ["next"] * iteration
            assert stamps == ["stamp"] * iteration
            stored = await Entry.objects.get(id=1)
            assert stored.value == f"next:{iteration}"
            assert stored.modified == datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=iteration)


@pytest.mark.asyncio
async def test_full_compilation_reuses_the_exact_values_of_the_cached_write():
    async with hare_test_context([__name__], db_url="sqlite+aiosqlite://:memory:"):
        await Entry.objects.create(id=1, value="initial")
        query = Entry.objects.filter(id=1).update(value="next")
        await query
        execution = query._get_execution_query(True)
        counts = len(conversions), len(stamps)
        execution._make_query_to_run()
        assert execution._compiled_statement is not None
        planned = execution._get_parameterized_sql()

        fresh = copy(execution)
        fresh._compiled_statement = None
        fresh._runs_compiled_statement = False
        fresh._make_query()
        assert fresh._get_parameterized_sql() == planned
        assert (len(conversions), len(stamps)) == counts
