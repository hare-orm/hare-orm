"""Postgres aggregates and functions: order_by of ArrayAgg/StringAgg/JSONBAgg, BitAnd/BitOr/BitXor,
the two-column statistics, TransactionNow and RandomUUID."""

import asyncio
import os
import uuid
from collections.abc import AsyncGenerator
from typing import Any

import pytest
import pytest_asyncio

from hare.contrib.test.helpers import hare_test_context, truncate_all_models
from hare.dialects.postgresql.functions.aggregates import ArrayAgg, BitAnd, BitOr, BitXor, JSONBAgg, StringAgg
from hare.dialects.postgresql.functions.datetime import TransactionNow
from hare.dialects.postgresql.functions.statistics import (
    Corr,
    CovarPop,
    RegrAvgX,
    RegrAvgY,
    RegrCount,
    RegrIntercept,
    RegrR2,
    RegrSlope,
    RegrSXX,
    RegrSXY,
    RegrSYY,
)
from hare.dialects.postgresql.functions.uuid import RandomUUID
from hare.exceptions import QueryError
from hare.query.expressions import F, Q
from hare.query.functions import Sum
from hare.query.functions.datetime import Now
from hare.transactions.transactions import Transactions
from tests.dialects.postgresql.conftest import skip_if_not_postgres
from tests.typed_row_models import TypedRow, TypedRowNote


@pytest_asyncio.fixture(scope="module")
async def aggregates_context() -> AsyncGenerator[Any]:
    skip_if_not_postgres()
    db_url = os.environ["HARE_TEST_DB"].replace("\\{", "{").replace("\\}", "}")
    db_url = db_url.format(uuid.uuid4().hex) if "{}" in db_url else db_url
    async with hare_test_context(["tests.typed_row_models"], db_url=db_url) as ctx:
        yield ctx


@pytest_asyncio.fixture
async def rows(aggregates_context: Any) -> AsyncGenerator[None]:
    for row_id, number, big_number, ratio, name in [
        (1, 12, 1, 1.0, "b"),
        (2, 10, 2, 2.0, "a"),
        (3, 6, 3, 4.0, "c"),
        (4, None, 4, 8.0, "z"),
    ]:
        await TypedRow.objects.create(id=row_id, number=number, big_number=big_number, ratio=ratio, name=name)
    for note_id, row_id in [(10, 1), (20, 1), (30, 2)]:
        await TypedRowNote.objects.create(id=note_id, row_id=row_id)
    yield
    await truncate_all_models()


@pytest.mark.asyncio
async def test_order_by_orders_the_collected_values(rows):
    def collected(aggregate):
        return TypedRow.objects.all().aggregate(value=aggregate)

    assert await collected(ArrayAgg("name", order_by="name")) == {"value": ["a", "b", "c", "z"]}
    assert await collected(ArrayAgg("name", order_by="-name")) == {"value": ["z", "c", "b", "a"]}
    assert await collected(ArrayAgg("name", order_by=F("ratio").desc())) == {"value": ["z", "c", "a", "b"]}
    assert await collected(ArrayAgg("name", order_by=["-number", "id"])) == {"value": ["z", "b", "a", "c"]}
    assert await collected(ArrayAgg("name", distinct=True, order_by="-name")) == {"value": ["z", "c", "b", "a"]}
    assert await collected(ArrayAgg("name", _filter=Q(id__lt=3), order_by="name")) == {"value": ["a", "b"]}
    assert await collected(StringAgg("name", "|", order_by="-id")) == {"value": "z|c|a|b"}
    assert await collected(JSONBAgg("name", order_by="name")) == {"value": ["a", "b", "c", "z"]}


@pytest.mark.asyncio
async def test_order_by_across_a_relation_and_in_the_query_shape(rows):
    by_note = TypedRow.objects.annotate(notes_desc=ArrayAgg("notes__id", order_by="-notes__id")).order_by("id")
    assert await by_note.values_list("notes_desc", flat=True) == [[20, 10], [30], [None], [None]]
    ascending = TypedRow.objects.all().aggregate(value=ArrayAgg("id", order_by="id"))
    descending = TypedRow.objects.all().aggregate(value=ArrayAgg("id", order_by="-id"))
    assert await ascending == {"value": [1, 2, 3, 4]}
    assert await descending == {"value": [4, 3, 2, 1]}


def test_order_by_is_rejected_where_order_doesnt_matter():
    with pytest.raises(QueryError, match="doesn't take order_by"):
        Sum("number", order_by="id")
    with pytest.raises(QueryError, match="takes field names"):
        ArrayAgg("name", order_by=[1])


@pytest.mark.asyncio
async def test_bit_aggregates(rows):
    assert await TypedRow.objects.all().aggregate(
        all_bits=BitAnd("number"),
        any_bits=BitOr("number"),
        odd_bits=BitXor("number"),
        distinct_odd_bits=BitXor("big_number", distinct=True),
        filtered=BitOr("number", _filter=Q(id__lt=3)),
    ) == {"all_bits": 0, "any_bits": 14, "odd_bits": 0, "distinct_odd_bits": 4, "filtered": 14}


@pytest.mark.asyncio
async def test_statistic_pair_aggregates(rows):
    result = await TypedRow.objects.all().aggregate(
        corr=Corr("ratio", "big_number"),
        covar_pop=CovarPop("ratio", "big_number"),
        covar_samp=CovarPop("ratio", "big_number", sample=True),
        avg_x=RegrAvgX("ratio", "big_number"),
        avg_y=RegrAvgY("ratio", "big_number"),
        count=RegrCount("number", "big_number"),
        intercept=RegrIntercept("ratio", "big_number"),
        r2=RegrR2("ratio", "big_number"),
        slope=RegrSlope("ratio", "big_number"),
        sxx=RegrSXX("ratio", "big_number"),
        sxy=RegrSXY("ratio", "big_number"),
        syy=RegrSYY("ratio", "big_number"),
    )
    assert result == pytest.approx(
        {
            "corr": 0.9591663046625439,
            "covar_pop": 2.875,
            "covar_samp": 11.5 / 3,
            "avg_x": 2.5,
            "avg_y": 3.75,
            "count": 3,
            "intercept": -2.0,
            "r2": 0.92,
            "slope": 2.3,
            "sxx": 5.0,
            "sxy": 11.5,
            "syy": 28.75,
        }
    )
    grouped = TypedRow.objects.annotate(count=RegrCount("number", "big_number")).group_by("name").order_by("name")
    assert await grouped.values_list("name", "count") == [("a", 1), ("b", 1), ("c", 1), ("z", 0)]


@pytest.mark.asyncio
async def test_random_uuid_is_generated_per_row(rows):
    await TypedRow.objects.all().update(uid=RandomUUID())
    generated = await TypedRow.objects.all().values_list("uid", flat=True)
    assert len(set(generated)) == 4
    assert all(isinstance(value, uuid.UUID) and value.version == 4 for value in generated)


@pytest.mark.asyncio
async def test_transaction_now_is_the_start_of_the_transaction(rows):
    async with Transactions.atomic():
        await TypedRow.objects.filter(id=1).update(at=TransactionNow())
        await asyncio.sleep(0.02)
        await TypedRow.objects.filter(id=2).update(at=TransactionNow())
        await TypedRow.objects.filter(id=3).update(at=Now())
    moments = dict(await TypedRow.objects.all().values_list("id", "at"))
    assert moments[1] == moments[2]
    assert moments[3] > moments[1]
