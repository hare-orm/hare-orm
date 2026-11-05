from datetime import timedelta

import pytest

from hare import Model, fields
from hare.contrib.test import hare_test_context

pytestmark = pytest.mark.sqlite_only


class Schedule(Model):
    start = fields.TimeDeltaField()
    end = fields.TimeDeltaField()
    pk = fields.CompositePrimaryKey("start", "end")
    deleted_at = fields.DatetimeField(null=True)

    class Meta:
        soft_delete_field = "deleted_at"


class Book(Model):
    schedule = fields.ForeignKeyField(
        "models.Schedule", on_delete=fields.SET_DEFAULT, db_default=timedelta(), db_constraint=False
    )


@pytest.mark.asyncio
async def test_composite_set_default_encodes_each_component_once():
    async with hare_test_context([__name__], db_url="sqlite+aiosqlite://:memory:"):
        fallback = await Schedule.objects.create(start=timedelta(), end=timedelta())
        original = await Schedule.objects.create(start=timedelta(hours=1), end=timedelta(hours=2))
        book = await Book.objects.create(schedule=original)
        await original.delete()
        stored = await Book.objects.get(pk=book.pk)
        relation = Book._meta.fields_map["schedule"]
        assert tuple(getattr(stored, name) for name in relation.source_fields) == fallback.pk
