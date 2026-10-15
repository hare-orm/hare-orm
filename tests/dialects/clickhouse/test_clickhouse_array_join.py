"""``ArrayJoin`` on ClickHouse - each row repeated with each element of an array (``ARRAY JOIN``): the
element and paths inside it read in ``values()``, filters and ``order_by()``, a row with an empty
array dropped or (``left=True``) kept once, counts and aggregates reading the repeated rows."""

import pytest
import pytest_asyncio

from hare.exceptions import QueryError
from hare.query.expressions import ArrayJoin
from hare.query.functions import Count, Sum
from tests.dialects.clickhouse.models import Shipment
from tests.dialects.clickhouse.test_clickhouse_containers import make_shipment


@pytest_asyncio.fixture
async def shipments(clickhouse_db):
    await Shipment.objects.bulk_create(
        [
            make_shipment(1, tags=["red", "blue"]),
            make_shipment(2, tags=["green"], items=[{"sku": "c-3", "quantity": 5}]),
            make_shipment(3, tags=[], items=[]),
        ]
    )


@pytest.mark.asyncio
async def test_each_element_with_its_row(shipments):
    rows = await Shipment.objects.alias(tag=ArrayJoin("tags")).order_by("id", "tag").values_list("id", "tag")
    assert rows == [(1, "blue"), (1, "red"), (2, "green")]
    # A row with an empty array is kept once with left=True - its element the type's default.
    rows = await (
        Shipment.objects.alias(tag=ArrayJoin("tags", left=True)).order_by("id", "tag").values_list("id", "tag")
    )
    assert rows == [(1, "blue"), (1, "red"), (2, "green"), (3, "")]


@pytest.mark.asyncio
async def test_filters_and_paths_inside_the_element(shipments):
    tagged = Shipment.objects.alias(tag=ArrayJoin("tags"))
    assert await tagged.filter(tag__startswith="r").values_list("id", flat=True) == [1]
    assert await tagged.filter(tag__in=["blue", "green"]).order_by("id").values_list("id", flat=True) == [1, 2]
    items = Shipment.objects.alias(item=ArrayJoin("items"))
    assert await items.order_by("id", "item__sku").values_list("id", "item__sku", "item__quantity") == [
        (1, "a-1", 2),
        (1, "b-7", 1),
        (2, "c-3", 5),
    ]
    assert await items.filter(item__quantity__gt=1).order_by("id").values_list("id", "item__sku") == [
        (1, "a-1"),
        (2, "c-3"),
    ]


@pytest.mark.asyncio
async def test_counts_and_aggregates_read_the_repeated_rows(shipments):
    tagged = Shipment.objects.alias(tag=ArrayJoin("tags"))
    assert await tagged.filter(tag__isnull=False).count() == 3
    totals = await Shipment.objects.alias(item=ArrayJoin("items")).aggregate(
        quantity=Sum("item__quantity"), lines=Count("id")
    )
    assert totals == {"quantity": 8, "lines": 3}
    grouped = await (
        Shipment.objects.alias(item=ArrayJoin("items"))
        .values("id")
        .annotate(quantity=Sum("item__quantity"))
        .order_by("id")
    )
    assert grouped == [{"id": 1, "quantity": 3}, {"id": 2, "quantity": 5}]


@pytest.mark.asyncio
async def test_only_an_array_is_joined(shipments):
    with pytest.raises(QueryError):
        await Shipment.objects.alias(tag=ArrayJoin("id")).values_list("tag")
    with pytest.raises(QueryError):
        ArrayJoin("tags", left="yes")
