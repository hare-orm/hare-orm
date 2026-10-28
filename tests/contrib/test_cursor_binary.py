"""Cursor pagination preserves arbitrary binary ordering values."""

import os

import pytest

from hare import fields
from hare.contrib.request_query import CursorPagination, OrderingConfig, RequestQuery
from hare.contrib.request_query.enums import CursorDirection
from hare.contrib.request_query.paginations.cursor import Cursor
from hare.contrib.test import hare_test_context
from hare.models import Model

REQUIRES = ("request-query",)


class BinaryEntry(Model):
    id = fields.IntField(primary_key=True)
    sort_key = fields.BinaryField()


@pytest.mark.database_independent
@pytest.mark.parametrize("value", [b"", b"ascii", b"\x00\x80\xff", None])
def test_a_binary_cursor_round_trip(value):
    cursor = Cursor(CursorDirection.NEXT, ("sort_key", "id"), (value, 3))
    assert Cursor.decode(cursor.encode(), (bytes, int), (True, False)) == cursor


@pytest.mark.asyncio
async def test_binary_pages_forward_and_back():
    async with hare_test_context([__name__], db_url=os.getenv("HARE_TEST_DB", "sqlite+aiosqlite://:memory:")):

        class BinaryEntries(RequestQuery[BinaryEntry]):
            class Meta:
                queryset = BinaryEntry.objects.all()
                ordering = OrderingConfig(default=("sort_key",))
                pagination = CursorPagination(default_limit=1)

        for row_id, value in enumerate((b"\x80", b"\x81", b"\xff"), 1):
            await BinaryEntry.objects.create(id=row_id, sort_key=value)
        assert await BinaryEntry.objects.order_by("sort_key").values_list("id", flat=True) == [1, 2, 3]
        first = await BinaryEntries().page()
        second = await BinaryEntries(cursor=first.next_cursor).page()
        third = await BinaryEntries(cursor=second.next_cursor).page()
        back = await BinaryEntries(cursor=third.previous_cursor).page()
        assert [[row.id for row in page.result] for page in (first, second, third, back)] == [[1], [2], [3], [2]]
        assert third.next_cursor is None
