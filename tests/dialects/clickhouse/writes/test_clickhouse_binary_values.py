"""A BinaryField holds any bytes on ClickHouse - the hex of them in a String column, read back exactly over
both drivers, whatever the bytes: UTF-8 text, bytes that aren't UTF-8, digits a text of hex is made of."""

import pytest

from hare.query.expressions import F, Value
from hare.query.functions import JSONObject
from tests.dialects.clickhouse.models import Attachment

VALUES = [b"", b"plain text", b"deadbeef", b"\x00\x01\xff", bytes(range(256)), "ünïcödé".encode()]


@pytest.mark.asyncio
async def test_any_bytes_read_back_exactly(clickhouse_db):
    await Attachment.objects.create(id=1, content=VALUES[0])
    await Attachment.objects.bulk_create(
        [Attachment(id=index, content=value) for index, value in enumerate(VALUES[1:], start=2)]
    )

    assert await Attachment.objects.order_by("id").values_list("content", flat=True) == VALUES
    assert [attachment.content for attachment in await Attachment.objects.order_by("id")] == VALUES
    await Attachment.objects.filter(id=2).update(content=b"\xfe\xfe")
    assert (await Attachment.objects.get(id=2)).content == b"\xfe\xfe"


@pytest.mark.asyncio
async def test_bytes_are_filtered_and_ordered_as_bytes(clickhouse_db):
    await Attachment.objects.bulk_create(
        [Attachment(id=1, content=b"\x02"), Attachment(id=2, content=b"\x01\xff"), Attachment(id=3, content=b"\x10")]
    )

    assert await Attachment.objects.filter(content=b"\x01\xff").values_list("id", flat=True) == [2]
    assert await Attachment.objects.filter(content__in=[b"\x02", b"\x10"]).order_by("id").values_list(
        "id", flat=True
    ) == [1, 3]
    assert await Attachment.objects.order_by("content").values_list("id", flat=True) == [2, 1, 3]
    assert await Attachment.objects.filter(id=2).annotate(card=JSONObject(content=F("content"))).values_list(
        "card", flat=True
    ) == [{"content": "01ff"}]


@pytest.mark.asyncio
async def test_a_literal_of_bytes_reads_back_as_the_bytes(clickhouse_db):
    await Attachment.objects.create(id=1, content=b"x")
    for value in VALUES:
        rows = await Attachment.objects.annotate(literal=Value(value)).values_list("literal", flat=True)
        assert rows == [value]
    # Compared with a column's bytes as the bytes they are.
    assert await Attachment.objects.filter(content=Value(b"x")).count() == 1
