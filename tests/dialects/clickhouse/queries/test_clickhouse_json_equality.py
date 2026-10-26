"""A JSONField compared whole - ``=`` and ``__not`` - as ClickHouse compares it: a JSON column with the
value cast to JSON, the key order not mattering; a String column of JSON text by the text."""

import pytest

from hare.contrib.test import assert_query_count
from hare.exceptions import ValidationError
from tests.dialects.clickhouse.models import Attachment

DOCUMENTS = [{"a": 2, "b": 1}, {"n": [[[]]], "x": "y"}, {"k": "v", "list": [1, "x", None], "flags": [True, False]}]


async def get_ids(**lookups):
    return await Attachment.objects.filter(**lookups).order_by("id").values_list("id", flat=True)


@pytest.mark.asyncio
async def test_a_document_equals_the_same_document(clickhouse_db):
    await Attachment.objects.bulk_create(
        [Attachment(id=index, document=document) for index, document in enumerate(DOCUMENTS, start=1)]
    )
    await Attachment.objects.create(id=9, document=None)

    assert await get_ids(document={"a": 2, "b": 1}) == [1]
    assert await get_ids(document={"n": [[[]]], "x": "y"}) == [2]
    assert await get_ids(document={"k": "v", "list": [1, "x", None], "flags": [True, False]}) == [3]
    assert await get_ids(document__not={"a": 2, "b": 1}) == [2, 3, 9]
    reordered = await get_ids(document={"b": 1, "a": 2})
    assert reordered == ([1] if Attachment._meta.connection.features.supports_json_type else [])


@pytest.mark.asyncio
async def test_a_boolean_beside_a_number_in_an_array_is_kept_or_refused(clickhouse_db):
    """A JSON column's mutation reads an array of a boolean and a number as numbers - an update would store
    true as 1, so no write of one is taken; a String column of JSON text keeps it as written."""
    if Attachment._meta.connection.features.supports_json_type:
        with pytest.raises(ValidationError, match="boolean beside a number or a text"):
            await Attachment.objects.create(id=1, document={"values": [1, 2.5, None, True]})
        with pytest.raises(ValidationError, match="boolean beside a number or a text"):
            await Attachment.objects.bulk_create([Attachment(id=2, document={"values": [False, 0]})])
        assert await Attachment.objects.count() == 0
        return
    await Attachment.objects.create(id=1, document={"values": [1, 2.5, None, True]})
    await Attachment.objects.bulk_create([Attachment(id=2, document={"values": [False, 0]})])

    assert await Attachment.objects.order_by("id").values_list("document", flat=True) == [
        {"values": [1, 2.5, None, True]},
        {"values": [False, 0]},
    ]


@pytest.mark.asyncio
async def test_an_empty_list_matches_no_document_or_every_one(clickhouse_db):
    await Attachment.objects.bulk_create([Attachment(id=1, document={"a": 1}), Attachment(id=2, document={"b": 2})])
    await Attachment.objects.create(id=3, document=None)
    assert await get_ids(document__in=[]) == []
    assert await get_ids(document__not_in=[]) == [1, 2, 3]
    assert await Attachment.objects.exclude(document__in=[]).count() == 3


@pytest.mark.asyncio
async def test_in_and_a_value_no_column_holds_is_refused(clickhouse_db):
    """__in is an equality of each document - no IN takes a JSON column; a number, a string or an array is no
    value a JSON column holds, refused as a write of it is."""
    if not Attachment._meta.connection.features.supports_json_type:
        pytest.skip("a String column of JSON text holds any JSON value - compared by its text")
    await Attachment.objects.bulk_create(
        [Attachment(id=index, document=document) for index, document in enumerate(DOCUMENTS, start=1)]
    )
    await Attachment.objects.create(id=9, document=None)

    assert await get_ids(
        document__in=[{"a": 2, "b": 1}, {"k": "v", "list": [1, "x", None], "flags": [True, False]}]
    ) == [
        1,
        3,
    ]
    assert await get_ids(document__not_in=[{"a": 2, "b": 1}]) == [2, 3, 9]
    async with assert_query_count(0):
        for value in (5, "plain", [1], True):
            for lookup in ("document", "document__not"):
                with pytest.raises(ValidationError, match="holds an object"):
                    await get_ids(**{lookup: value})
            for lookup in ("document__in", "document__not_in"):
                with pytest.raises(ValidationError, match="holds an object"):
                    await get_ids(**{lookup: [{"a": 2, "b": 1}, value]})
