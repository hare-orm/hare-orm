"""Window(frame=...): RowRange counts rows around the current one, ValueRange ordering values - every
edge (unbounded, n before, the current row, n after) on every database, value functions included,
and the frames a window can't take."""

from __future__ import annotations

import pytest

from hare.exceptions import QueryError
from hare.query.expressions import RowRange, ValueRange, Window
from hare.query.functions.window import FirstValue, Lag, LastValue, RowNumber, Sum
from tests.testmodels import IntFields


async def get_window_values(values: list[int], window: Window, *, order_by=("intnum", "id")) -> list:
    for row_id, value in enumerate(values, start=1):
        await IntFields.objects.create(id=row_id, intnum=value, intnum_null=row_id % 2)
    rows = await IntFields.objects.all().values("id", framed=window).order_by(*order_by)
    return [row["framed"] for row in rows]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("frame", "expected"),
    [
        (RowRange(start=-1, end=0), [10, 30, 50, 70, 90]),
        (RowRange(start=-1, end=1), [30, 60, 90, 120, 90]),
        (RowRange(start=None, end=0), [10, 30, 60, 100, 150]),
        (RowRange(start=0, end=None), [150, 140, 120, 90, 50]),
        (RowRange(start=1, end=2), [50, 70, 90, 50, None]),
        (RowRange(start=-2, end=-1), [None, 10, 30, 50, 70]),
        (RowRange(), [150, 150, 150, 150, 150]),
    ],
    ids=lambda value: repr(value) if isinstance(value, RowRange) else "",
)
async def test_a_row_range_sums_the_rows_around_the_current_one(db, frame, expected):
    window = Window(Sum("intnum"), order_by=["intnum", "id"], frame=frame)
    assert await get_window_values([10, 20, 30, 40, 50], window) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("frame", "expected"),
    [
        # Peers - rows of the same value - are in each other's frame.
        (ValueRange(start=None, end=0), [10, 50, 50, 90]),
        (ValueRange(start=-10, end=10), [50, 50, 50, 40]),
        (ValueRange(start=0, end=0), [10, 40, 40, 40]),
        (ValueRange(), [90, 90, 90, 90]),
    ],
    ids=lambda value: repr(value) if isinstance(value, ValueRange) else "",
)
async def test_a_value_range_sums_the_rows_whose_value_is_near(db, frame, expected):
    window = Window(Sum("intnum"), order_by=["intnum"], frame=frame)
    assert await get_window_values([10, 20, 20, 40], window) == expected


@pytest.mark.asyncio
async def test_a_row_range_of_several_orderings_and_a_partition(db):
    window = Window(Sum("intnum"), partition_by=["intnum_null"], order_by=["intnum", "id"], frame=RowRange(-1, 0))
    # intnum_null alternates 1, 0, 1, 0, 1 - the partitions are 10/30/50 and 20/40.
    assert await get_window_values([10, 20, 30, 40, 50], window) == [10, 20, 40, 60, 80]


@pytest.mark.asyncio
async def test_value_functions_take_a_frame(db):
    first_of_previous = Window(FirstValue("intnum"), order_by=["intnum", "id"], frame=RowRange(-1, 0))
    assert await get_window_values([10, 20, 30], first_of_previous) == [10, 10, 20]
    await IntFields.objects.all().delete()
    # LastValue's own whole-partition frame gives way to the one given.
    last_of_current = Window(LastValue("intnum"), order_by=["intnum", "id"], frame=RowRange(-1, 0))
    assert await get_window_values([10, 20, 30], last_of_current) == [10, 20, 30]


@pytest.mark.asyncio
async def test_windows_differing_only_in_their_frame_each_keep_their_own(db):
    for row_id, value in enumerate([10, 20, 30], start=1):
        await IntFields.objects.create(id=row_id, intnum=value)
    for frame, expected in ((RowRange(-1, 0), [10, 30, 50]), (RowRange(0, 1), [30, 50, 30]), (None, [10, 30, 60])):
        rows = (
            await IntFields.objects.all()
            .values("id", framed=Window(Sum("intnum"), order_by=["intnum"], frame=frame))
            .order_by("intnum")
        )
        assert [row["framed"] for row in rows] == expected


@pytest.mark.asyncio
async def test_the_frames_a_window_cant_take(db):
    await IntFields.objects.create(id=1, intnum=1)
    with pytest.raises(QueryError, match="takes an aggregate or a value function, not RowNumber"):
        await IntFields.objects.all().values(framed=Window(RowNumber(), order_by=["id"], frame=RowRange(-1, 0)))
    with pytest.raises(QueryError, match="takes an aggregate or a value function, not Lag"):
        await IntFields.objects.all().values(framed=Window(Lag("intnum"), order_by=["id"], frame=RowRange(-1, 0)))
    with pytest.raises(QueryError, match="order the window by exactly one field"):
        await IntFields.objects.all().values(
            framed=Window(Sum("intnum"), order_by=["intnum", "id"], frame=ValueRange(-5, 0))
        )
    # Without an offset, a value range takes any ordering.
    rows = await IntFields.objects.all().values(
        framed=Window(Sum("intnum"), order_by=["intnum", "id"], frame=ValueRange(None, 0))
    )
    assert rows == [{"framed": 1}]


@pytest.mark.parametrize(
    ("make_frame", "message"),
    [
        (lambda: RowRange(start="a"), "takes None or an int from"),
        (lambda: RowRange(end=1.5), "takes None or an int from"),
        (lambda: ValueRange(start=True), "takes None or an int from"),
        (lambda: RowRange(start=-(2**63)), "takes None or an int from"),
        (lambda: ValueRange(end=2**63), "takes None or an int from"),
        (lambda: RowRange(start=2, end=1), "start \\(2\\) comes after end \\(1\\)"),
        (lambda: Window(Sum("intnum"), frame=(-1, 0)), "takes a RowRange or a ValueRange"),
    ],
)
def test_a_wrong_frame_is_refused(make_frame, message):
    with pytest.raises(QueryError, match=message):
        make_frame()


def test_frames_compare_by_their_type_and_edges():
    assert RowRange(-1, 0) == RowRange(-1, 0)
    assert RowRange(-1, 0) != ValueRange(-1, 0)
    assert len({RowRange(-1, 0), RowRange(-1, 0), RowRange(0, 1)}) == 2
    assert repr(ValueRange(None, 0)) == "ValueRange(start=None, end=0)"
