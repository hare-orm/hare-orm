"""StdDev/Variance aggregates and window functions, CumeDist, PercentRank and NthValue."""

import datetime
import math
import statistics
from decimal import Decimal
from typing import Any

import pytest

from hare.exceptions import QueryError
from hare.query.expressions import Q, Window
from hare.query.functions import StdDev, Variance
from hare.query.functions.window import (
    CumeDist,
    NthValue,
    PercentRank,
    StdDev as WindowStdDev,
    Variance as WindowVariance,
)
from tests.testmodels import ExpressionTypeRow

ROWS = (
    ("a", 3, 0.5, "1.25"),
    ("a", 5, 1.5, "2.50"),
    ("a", 5, 2.0, "2.50"),
    ("b", 10, -1.0, "7.75"),
    ("b", 4, 3.25, "0.10"),
    ("c", 7, 0.0, "3.00"),
)


async def create_rows() -> list[ExpressionTypeRow]:
    return [
        await ExpressionTypeRow.objects.create(
            num=num,
            fl=fl,
            dec=Decimal(dec),
            dec3=Decimal("0"),
            d=datetime.date(2020, 1, 1),
            dt=datetime.datetime(2020, 1, 1, tzinfo=datetime.UTC),
            td=datetime.timedelta(0),
            s=group,
            flag=True,
            num_null=num if num != 5 else None,
        )
        for group, num, fl, dec in ROWS
    ]


def expected_statistic(values: list[Any], sample: bool, deviation: bool) -> float | None:
    present = [float(value) for value in values if value is not None]
    if len(present) < (2 if sample else 1):
        return None
    variance = statistics.variance(present) if sample else statistics.pvariance(present)
    return math.sqrt(variance) if deviation else variance


def assert_close(got: Any, expected: float | None) -> None:
    if expected is None:
        assert got is None
    else:
        assert math.isclose(float(got), expected, rel_tol=1e-9, abs_tol=1e-12), (got, expected)


@pytest.mark.asyncio
@pytest.mark.parametrize("function, deviation", [(StdDev, True), (Variance, False)])
@pytest.mark.parametrize("sample", [False, True])
@pytest.mark.parametrize("column, position", [("num", 1), ("fl", 2), ("dec", 3)])
async def test_statistic_aggregate_matches_python(db, function, deviation, sample, column, position):
    await create_rows()
    result = await ExpressionTypeRow.objects.all().aggregate(value=function(column, sample=sample))
    assert_close(result["value"], expected_statistic([row[position] for row in ROWS], sample, deviation))


@pytest.mark.asyncio
async def test_statistic_aggregate_result_types(db):
    await create_rows()
    result = await ExpressionTypeRow.objects.all().aggregate(
        of_int=StdDev("num"), of_float=Variance("fl"), of_decimal=Variance("dec", sample=True)
    )
    assert type(result["of_int"]) is float
    assert type(result["of_float"]) is float
    assert type(result["of_decimal"]) is Decimal


@pytest.mark.asyncio
async def test_statistic_aggregate_distinct_and_filter(db):
    await create_rows()
    result = await ExpressionTypeRow.objects.all().aggregate(
        distinct=Variance("num", distinct=True),
        filtered=StdDev("num", sample=True, _filter=Q(s="a")),
    )
    assert_close(result["distinct"], statistics.pvariance([3, 5, 10, 4, 7]))
    assert_close(result["filtered"], statistics.stdev([3, 5, 5]))


@pytest.mark.asyncio
async def test_statistic_aggregate_skips_nulls_and_needs_enough_values(db):
    await create_rows()
    result = await ExpressionTypeRow.objects.all().aggregate(
        with_nulls=Variance("num_null"),
        no_rows=Variance("num", _filter=Q(s="missing")),
        one_row_sample=Variance("num", sample=True, _filter=Q(s="c")),
        one_row_population=Variance("num", _filter=Q(s="c")),
    )
    assert_close(result["with_nulls"], statistics.pvariance([3, 10, 4, 7]))
    assert result["no_rows"] is None
    assert result["one_row_sample"] is None
    assert_close(result["one_row_population"], 0.0)


@pytest.mark.asyncio
async def test_statistic_aggregate_grouped(db):
    await create_rows()
    grouped = dict(
        await ExpressionTypeRow.objects.annotate(value=StdDev("fl", sample=True))
        .group_by("s")
        .values_list("s", "value")
    )
    for group in ("a", "b", "c"):
        assert_close(grouped[group], expected_statistic([row[2] for row in ROWS if row[0] == group], True, True))


@pytest.mark.asyncio
@pytest.mark.parametrize("function, deviation", [(WindowStdDev, True), (WindowVariance, False)])
async def test_statistic_window_over_partition(db, function, deviation):
    rows = await create_rows()
    # The population then the sample statistic - one query shape apart, never sharing cached SQL.
    for sample in (False, True):
        values = dict(
            await ExpressionTypeRow.objects.annotate(
                value=Window(function("dec", sample=sample), partition_by=["s"])
            ).values_list("id", "value")
        )
        for row, source in zip(rows, ROWS, strict=True):
            group_values = [other[3] for other in ROWS if other[0] == source[0]]
            assert_close(values[row.id], expected_statistic(group_values, sample, deviation))


@pytest.mark.asyncio
async def test_statistic_aggregate_computed_over_window(db):
    rows = await create_rows()
    values = dict(
        await ExpressionTypeRow.objects.annotate(
            value=Window(Variance("num", sample=True, _filter=Q(num__gt=3)), partition_by=["s"])
        ).values_list("id", "value")
    )
    assert_close(values[rows[0].id], statistics.variance([5, 5]))
    assert_close(values[rows[3].id], statistics.variance([10, 4]))
    assert values[rows[5].id] is None


@pytest.mark.asyncio
async def test_statistic_window_running(db):
    rows = await create_rows()
    values = dict(
        await ExpressionTypeRow.objects.annotate(value=Window(WindowStdDev("fl"), order_by=["id"])).values_list(
            "id", "value"
        )
    )
    for position, row in enumerate(rows):
        assert_close(values[row.id], statistics.pstdev([source[2] for source in ROWS[: position + 1]]))


@pytest.mark.asyncio
async def test_cume_dist_and_percent_rank(db):
    rows = await create_rows()
    queryset = ExpressionTypeRow.objects.annotate(
        cume=Window(CumeDist(), order_by=["num"]), percent=Window(PercentRank(), order_by=["num"])
    )
    values = {row_id: (cume, percent) for row_id, cume, percent in await queryset.values_list("id", "cume", "percent")}
    # num: 3, 5, 5, 10, 4, 7 - the two 5s are peers.
    expected = [(1 / 6, 0.0), (4 / 6, 2 / 5), (4 / 6, 2 / 5), (1.0, 1.0), (2 / 6, 1 / 5), (5 / 6, 4 / 5)]
    for row, (cume, percent) in zip(rows, expected, strict=True):
        assert type(values[row.id][0]) is float
        assert math.isclose(values[row.id][0], cume)
        assert math.isclose(values[row.id][1], percent)


@pytest.mark.asyncio
async def test_percent_rank_of_one_row_partition_is_zero(db):
    rows = await create_rows()
    values = dict(
        await ExpressionTypeRow.objects.annotate(
            value=Window(PercentRank(), partition_by=["s"], order_by=["num"])
        ).values_list("id", "value")
    )
    assert values[rows[5].id] == 0.0


@pytest.mark.asyncio
async def test_nth_value_over_whole_partition(db):
    rows = await create_rows()
    for nth, expected in (
        (2, [Decimal("2.50")] * 3 + [Decimal("0.10")] * 2 + [None]),
        (3, [Decimal("2.50")] * 3 + [None] * 3),
    ):
        values = dict(
            await ExpressionTypeRow.objects.annotate(
                value=Window(NthValue("dec", nth), partition_by=["s"], order_by=["id"])
            ).values_list("id", "value")
        )
        assert [values[row.id] for row in rows] == expected


@pytest.mark.parametrize("nth", [0, -1, 1.5, True, "2"])
def test_nth_value_rejects_invalid_nth(nth):
    with pytest.raises(QueryError, match="nth must be an integer from 1"):
        NthValue("dec", nth)
