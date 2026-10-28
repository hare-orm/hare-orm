"""A condition as a value of each row on ClickHouse - its 0/1 read as a bool, NULL where a Nullable column
makes it NULL; filtered and ordered by, selected by values()."""

import pytest

from hare.query.expressions import Q
from tests.dialects.clickhouse.models import Device


async def create_devices() -> None:
    for device_id, city in ((1, None), (2, "Berlin"), (3, "Paris")):
        await Device.objects.create(
            id=device_id,
            tiny=device_id,
            big=1,
            ratio=1.0,
            code="abcd",
            country="DE",
            city=city,
            status="online",
            address="10.0.0.1",
            labels=[],
        )


@pytest.mark.asyncio
async def test_a_condition_is_a_bool_value_of_each_row(clickhouse_db):
    await create_devices()
    queryset = Device.objects.annotate(has_city=Q(city__isnull=False), in_berlin=Q(city="Berlin")).order_by("id")

    assert await queryset.values_list("has_city", "in_berlin") == [(False, None), (True, True), (True, False)]
    assert await queryset.filter(has_city=True).values_list("id", flat=True) == [2, 3]
    assert await Device.objects.order_by("id").values("id", small=~Q(tiny__gt=1)) == [
        {"id": 1, "small": True},
        {"id": 2, "small": False},
        {"id": 3, "small": False},
    ]


@pytest.mark.asyncio
async def test_a_comparison_condition_is_a_bool_compared_with_one(clickhouse_db):
    await create_devices()
    queryset = Device.objects.annotate(above_one=Q(tiny__gt=1)).order_by("id")

    assert await queryset.filter(above_one=True).values_list("id", flat=True) == [2, 3]
    values = await queryset.values_list("above_one", flat=True)
    assert values == [False, True, True]
    assert {type(value) for value in values} == {bool}
