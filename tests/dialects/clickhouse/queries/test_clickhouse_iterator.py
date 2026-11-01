"""iterator() on ClickHouse ordered by a float holding NaN - every comparison with NaN is false there, so
the pages follow one another by OFFSET and no row is lost."""

import pytest

from hare.query.expressions import F
from tests.dialects.clickhouse.models import Device, Player, StatusArray

#: The orderings of a float holding NaN - ClickHouse puts NaN between the numbers and the NULLs, on the side
#: the NULLs go to: an explicit NULL placement moves it.
FLOAT_ORDERINGS = pytest.mark.parametrize(
    "ordering",
    [
        "rating",
        "-rating",
        F("rating").asc(nulls_first=True),
        F("rating").asc(nulls_last=True),
        F("rating").desc(nulls_first=True),
        F("rating").desc(nulls_last=True),
    ],
    ids=["asc", "desc", "asc-nulls-first", "asc-nulls-last", "desc-nulls-first", "desc-nulls-last"],
)


@FLOAT_ORDERINGS
@pytest.mark.asyncio
async def test_an_iterator_ordered_by_a_float_with_nan_reads_every_row(clickhouse_db, ordering):
    """A keyset page boundary compared the float with < and > - the rows of NaN and those after them were
    lost."""
    for player_id, rating in ((1, 1.5), (2, float("nan")), (3, None), (4, float("nan")), (5, -2.0)):
        await Player.objects.create(id=player_id, name=f"p{player_id}", rating=rating)
    query = Player.objects.order_by(ordering, "id")
    assert [player.id async for player in query.iterator(chunk_size=1)] == [player.id for player in await query]


@FLOAT_ORDERINGS
@pytest.mark.asyncio
async def test_a_cursor_over_a_float_with_nan_reads_every_row_forward_and_back(clickhouse_db, ordering):
    """The boundary compared the float with < and > - after_cursor() lost the rows of NaN and those after
    them, and RequestQuery's cursor pages with it; then it put NaN after the numbers ascending whatever the
    NULLs' side - an explicit nulls_first lost the rows after NaN."""
    for player_id, rating in ((1, 1.5), (2, float("nan")), (3, None), (4, float("nan")), (5, -2.0)):
        await Player.objects.create(id=player_id, name=f"p{player_id}", rating=rating)
    query = Player.objects.order_by(ordering, "id")
    expected = [player.id for player in await query]
    forward = []
    page = await query.limit(1)
    for _ in range(len(expected) + 1):
        if not page:
            break
        forward.append(page[0].id)
        page = await query.after_cursor(page[0].rating, page[0].id).limit(1)
    assert forward == expected
    backward = []
    last = (await query)[-1]
    page = await query.before_cursor(last.rating, last.id).limit(1)
    for _ in range(len(expected)):
        if not page:
            break
        backward.append(page[0].id)
        page = await query.before_cursor(page[0].rating, page[0].id).limit(1)
    assert backward == list(reversed(expected[:-1]))


@pytest.mark.parametrize("ordering", ["ratio", "-ratio"])
@pytest.mark.asyncio
async def test_a_cursor_over_a_float32_moves_past_each_row(clickhouse_db, ordering):
    """The boundary was bound as a float64 - 0.1 as a float64 is less than the float32 0.1 the column holds,
    so after_cursor() read the boundary's own row again and iterator() repeated it."""
    for device_id, ratio in ((1, 0.1), (2, 0.1), (3, 0.2)):
        await Device.objects.create(
            id=device_id,
            tiny=1,
            big=1,
            ratio=ratio,
            code="abcd",
            country="DE",
            status="online",
            address="10.0.0.1",
            labels=[],
        )
    query = Device.objects.order_by(ordering, "id")
    rows = await query
    expected = [device.id for device in rows]
    iterated: list[int] = []
    async for device in query.iterator(chunk_size=1):
        iterated.append(device.id)
        if len(iterated) > len(expected):
            break
    assert iterated == expected
    assert [device.id for device in await query.after_cursor(rows[0].ratio, rows[0].id)] == expected[1:]
    assert [device.id for device in await query.before_cursor(rows[-1].ratio, rows[-1].id)] == expected[:-1]


@pytest.mark.parametrize("ordering", ["statuses", "-statuses", "statuses_copy", "-statuses_copy"])
@pytest.mark.asyncio
async def test_an_iterator_ordered_by_an_enum_array_or_its_generated_copy_reads_every_row(clickhouse_db, ordering):
    """A generated array of enums took the wrapper's comparisons for its own - the keyset repeated rows
    ascending and skipped them descending."""
    for array_id, status in ((1, "offline"), (2, "online"), (3, "offline"), (4, "online")):
        await StatusArray.objects.create(id=array_id, statuses=[status])
    query = StatusArray.objects.order_by(ordering, "id")
    expected = [row.id for row in await query]
    iterated: list[int] = []
    async for row in query.iterator(chunk_size=1):
        iterated.append(row.id)
        if len(iterated) > len(expected):
            break
    assert iterated == expected
