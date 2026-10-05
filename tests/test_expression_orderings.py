"""Ordering by an expression - ``order_by()``, ``latest()``/``earliest()``, a window's partitions and
ordering - with its direction and NULL placement, the plan of a query reused with other values."""

import pytest

from hare.exceptions import QueryError
from hare.query.expressions import F, Value, Window
from hare.query.functions import Coalesce, Lower, window as window_functions
from tests.testmodels import CharFields, ExpressionOrdered, IntFields


async def make_numbers():
    for index, (number, nullable) in enumerate([(3, None), (1, 5), (2, 4)], start=1):
        await IntFields.objects.create(id=index, intnum=number, intnum_null=nullable)


async def ids(queryset):
    return [row.id for row in await queryset]


@pytest.mark.asyncio
async def test_order_by_an_expression(db):
    await make_numbers()
    assert await ids(IntFields.objects.order_by(F("intnum") * -1)) == [1, 3, 2]
    assert await ids(IntFields.objects.order_by((F("intnum") * -1).desc())) == [2, 3, 1]
    assert await ids(IntFields.objects.order_by(F("intnum_null").desc(nulls_last=True))) == [2, 3, 1]
    assert await ids(IntFields.objects.order_by(Coalesce("intnum_null", Value(0)).asc(), "id")) == [1, 3, 2]
    assert await ids(IntFields.objects.order_by(F("intnum"))) == [2, 3, 1]


@pytest.mark.asyncio
async def test_an_ordered_query_takes_another_value_of_its_expression(db):
    await make_numbers()
    by_zero = IntFields.objects.order_by(Coalesce("intnum_null", Value(0)).asc())
    assert await ids(by_zero) == [1, 3, 2]
    assert await ids(IntFields.objects.order_by(Coalesce("intnum_null", Value(9)).asc())) == [3, 2, 1]
    assert await ids(IntFields.objects.order_by(Coalesce("intnum_null", Value(0)).asc())) == [1, 3, 2]


@pytest.mark.asyncio
async def test_single_rows_and_reversal_follow_the_expression(db):
    await make_numbers()
    ordered = IntFields.objects.order_by((F("intnum") * -1).asc())
    assert (await ordered.first()).id == 1
    assert (await ordered.last()).id == 2
    assert await ids(ordered.reverse()) == [2, 3, 1]
    assert await ids(ordered[1:3]) == [3, 2]
    assert (await IntFields.objects.latest(F("intnum") * -1)).id == 2
    assert (await IntFields.objects.earliest((F("intnum") * -1).asc())).id == 1
    assert await ordered.count() == 3


@pytest.mark.asyncio
async def test_a_window_partitions_and_orders_by_expressions(db):
    await make_numbers()
    ranked = (
        await IntFields.objects.order_by("id")
        .annotate(
            rank=Window(window_functions.RowNumber(), order_by=[(F("intnum") * -1).asc()]),
            in_group=Window(window_functions.Count("id"), partition_by=[Coalesce("intnum_null", Value(5))]),
        )
        .values_list("id", "rank", "in_group")
    )
    assert ranked == [(1, 1, 2), (2, 3, 2), (3, 2, 1)]


@pytest.mark.asyncio
async def test_text_ordered_by_an_expression_of_it(db):
    for index, text in enumerate(["b", "A", "c"], start=1):
        await CharFields.objects.create(id=index, char=text)
    assert await ids(CharFields.objects.order_by(Lower("char"))) == [2, 1, 3]
    assert await ids(CharFields.objects.order_by(Lower("char").desc())) == [3, 1, 2]


@pytest.mark.asyncio
async def test_a_combined_query_orders_by_names_alone(db):
    with pytest.raises(QueryError, match="ordering by an expression"):
        IntFields.objects.filter(id=1).union(IntFields.objects.filter(id=2)).order_by(F("intnum") * -1)


async def make_ordered():
    for index, (name, weight) in enumerate([("b", 1), ("A", 5), (None, 9), ("a", 7)], start=1):
        await ExpressionOrdered.objects.create(id=index, name=name, weight=weight)


@pytest.mark.asyncio
async def test_meta_ordering_by_expressions_orders_every_query(db):
    await make_ordered()
    objects = ExpressionOrdered.objects
    assert await ids(objects.all()) == [1, 4, 2, 3]
    assert await ids(objects.all()) == [1, 4, 2, 3]
    assert await objects.values_list("id", flat=True) == [1, 4, 2, 3]
    assert (await objects.first()).id == 1
    assert (await objects.last()).id == 3
    assert await ids(objects.reverse()) == [3, 2, 4, 1]
    assert await ids(objects.all()[1:3]) == [4, 2]
    assert [row.id async for row in objects.all().iterator(chunk_size=1)] == [1, 4, 2, 3]
    assert await objects.count() == 4
    assert await ids(objects.order_by("id")) == [1, 2, 3, 4]


@pytest.mark.asyncio
async def test_meta_ordering_by_expressions_orders_a_limited_update(db):
    await make_ordered()
    assert await ExpressionOrdered.objects.all().limit(1).update(weight=F("weight") + 100) == 1
    assert await ExpressionOrdered.objects.order_by("id").values_list("weight", flat=True) == [101, 5, 9, 7]
