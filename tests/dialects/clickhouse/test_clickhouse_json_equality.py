"""A JSONField compared whole - ``=``, ``__not``, ``__in``, ``__not_in`` - as PostgreSQL's jsonb compares:
the key order and the spelling of a number written don't matter, on a server with the JSON type, whether
its column is a JSON or a String one."""

import pytest

from tests.dialects.clickhouse.models import Attachment

DOCUMENTS = [{"a": 2, "b": 1}, {"n": [[[]]], "x": "y"}, {"f": 1.0}, {"k": "v", "list": [1, "x", True]}]


@pytest.fixture
def json_type_server(clickhouse_db):
    if not getattr(Attachment._meta.connection.dialect, "parses_json_type", False):
        pytest.skip("the server has no JSON type - a JSONField is compared by its text")


async def get_ids(**lookups):
    return await Attachment.objects.filter(**lookups).order_by("id").values_list("id", flat=True)


@pytest.mark.asyncio
async def test_a_document_equals_one_of_another_key_order_and_number_spelling(json_type_server):
    await Attachment.objects.bulk_create(
        [Attachment(id=index, document=document) for index, document in enumerate(DOCUMENTS, start=1)]
    )
    await Attachment.objects.create(id=9, document=None)

    assert await get_ids(document={"b": 1, "a": 2.0}) == [1]
    assert await get_ids(document={"x": "y", "n": [[[]]]}) == [2]
    assert await get_ids(document={"f": 1}) == [3]
    assert await get_ids(document={"list": [1, "x", True], "k": "v"}) == [4]
    assert await get_ids(document__not={"a": 2, "b": 1}) == [2, 3, 4, 9]
    assert await get_ids(document__in=[{"b": 1, "a": 2}, {"f": 1}]) == [1, 3]
    assert await get_ids(document__in=[{"f": 1}, None]) == [3, 9]
    assert await get_ids(document__not_in=[{"b": 1, "a": 2}, {"f": 1}]) == [2, 4, 9]
    assert await get_ids(document__not_in=[{"f": 1}, None]) == [1, 2, 4]


@pytest.mark.asyncio
async def test_a_boolean_in_an_array_of_numbers_stays_a_boolean(clickhouse_db):
    await Attachment.objects.create(id=1, document={"values": [1, 2.5, None, True]})
    await Attachment.objects.bulk_create([Attachment(id=2, document={"values": [False, 0]})])

    assert await Attachment.objects.order_by("id").values_list("document", flat=True) == [
        {"values": [1, 2.5, None, True]},
        {"values": [False, 0]},
    ]
