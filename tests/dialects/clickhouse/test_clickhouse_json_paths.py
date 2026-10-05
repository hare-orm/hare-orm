"""Filters on a path into a JSON field on ClickHouse - a JSON column, or the document's text on a server
without the JSON type: a value at the path compares and orders as the same JSON value does on
PostgreSQL, however each side writes it; a JSON column keeps no null, which reads as a missing path."""

import pytest
import pytest_asyncio

from hare.query.expressions import F
from tests.dialects.clickhouse.models import Player


@pytest_asyncio.fixture
async def documents(clickhouse_db):
    await Player.objects.create(
        id=1,
        name="http://x/y",
        data={"url": "http://x/y", "ratio": 1.0, "big": 1e20, "count": 10, "nothing": None, "flag": True},
    )
    await Player.objects.create(id=2, name="other", data={"url": "https://z", "ratio": 2.5, "count": 9})
    await Player.objects.create(id=3, name="text", data={"count": "a", "ratio": [1, 2]})


async def ids(**filters):
    return await Player.objects.filter(**filters).order_by("id").values_list("id", flat=True)


@pytest.mark.asyncio
async def test_a_text_with_a_slash_matches(documents):
    assert await ids(data__url="http://x/y") == [1]
    assert await ids(data__url__startswith="http://") == [1]
    assert await ids(data__url__in=["http://x/y", "https://z"]) == [1, 2]


@pytest.mark.asyncio
async def test_a_number_matches_whatever_its_text(documents):
    assert await ids(data__ratio=1.0) == [1]
    assert await ids(data__ratio=1) == [1]
    assert await ids(data__big=1e20) == [1]
    assert await ids(data__ratio__in=[1, 2.5]) == [1, 2]
    assert await ids(data__ratio__not_in=[1]) == [2, 3]


@pytest.mark.asyncio
async def test_numbers_compare_by_value_not_by_text(documents):
    assert await ids(data__count__gt=9) == [1]
    assert await ids(data__count__gte=9) == [1, 2]
    assert await ids(data__count__lt=10) == [2, 3]
    assert await ids(data__count__range=(9, 10)) == [1, 2]
    # A None bound is the JSON null, below every value - as on PostgreSQL.
    assert await ids(data__count__range=(9, None)) == []
    assert await ids(data__count__range=(None, 9)) == [2, 3]


@pytest.mark.asyncio
async def test_values_order_as_jsonb(documents):
    # A string is below every number, a missing value counts as no value.
    assert await Player.objects.order_by("data__count").values_list("id", flat=True) == [3, 2, 1]
    assert await Player.objects.order_by("-data__ratio", "id").values_list("id", flat=True) == [3, 2, 1]


@pytest.mark.asyncio
async def test_a_json_null_is_a_value_and_a_missing_key_none(documents):
    if Player._meta.connection.features.supports_json_type:
        # A JSON column drops a null - the path reads as a missing one.
        assert await ids(data__nothing=None) == [1, 2, 3]
        assert await ids(data__nothing__isnull=True) == [1, 2, 3]
    else:
        assert await ids(data__nothing=None) == [1]
        assert await ids(data__nothing__isnull=True) == [2, 3]
    assert await ids(data__count__isnull=False) == [1, 2, 3]
    assert await ids(data__flag=True) == [1]


@pytest.mark.asyncio
async def test_a_path_compared_with_a_column_of_text(documents):
    assert await ids(data__url=F("name")) == [1]
