"""Random() - a float from 0 to 1, new for every row - and order_by("?") ordering the rows at random,
alone, beside other orderings, in values() and with a slice."""

from __future__ import annotations

import pytest

from hare.query.functions import Random
from tests.testmodels import IntFields


async def create_rows(count: int) -> None:
    for row_id in range(1, count + 1):
        await IntFields.objects.create(id=row_id, intnum=row_id % 3)


@pytest.mark.asyncio
async def test_random_is_a_float_from_zero_to_one_new_for_every_row(db):
    await create_rows(40)
    rows = await IntFields.objects.all().values("id", chance=Random())
    chances = [row["chance"] for row in rows]
    assert all(isinstance(chance, float) and 0 <= chance < 1 for chance in chances)
    assert len(set(chances)) > 30


@pytest.mark.asyncio
async def test_order_by_question_mark_orders_the_rows_at_random(db):
    await create_rows(40)
    orders = {tuple(row.id for row in await IntFields.objects.order_by("?")) for _ in range(5)}
    assert len(orders) > 1
    assert all(sorted(order) == list(range(1, 41)) for order in orders)


@pytest.mark.asyncio
async def test_a_random_ordering_beside_others_in_values_and_sliced(db):
    await create_rows(30)
    rows = await IntFields.objects.order_by("intnum", "?").values("id", "intnum")
    assert [row["intnum"] for row in rows] == sorted(row["intnum"] for row in rows)
    assert set(rows[0]) == {"id", "intnum"}
    picked = await IntFields.objects.order_by("?").limit(3).values_list("id", flat=True)
    assert len(picked) == 3 and len(set(picked)) == 3
    assert await IntFields.objects.order_by("?").first() is not None
