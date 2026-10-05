"""Where NULL sorts on ClickHouse - first in a descending ordering, as on PostgreSQL, though ClickHouse
puts it last in both directions: in a page after a cursor, in a reversed ordering, in a window's range
of values around a NULL and in an ordered ``ArrayAgg``."""

import pytest

from hare.query.expressions import F, Value, ValueRange, Window
from hare.query.functions import ArrayAgg
from hare.query.functions.window import Sum
from tests.dialects.clickhouse.models import Player

RATINGS = {1: 1.0, 2: 1.0, 3: 4.0, 4: None, 5: 13.0, 6: 13.0, 7: None}


async def create_players():
    await Player.objects.bulk_create(
        [Player(id=player_id, name=f"player {player_id}", rating=rating) for player_id, rating in RATINGS.items()]
    )


async def get_ids(queryset):
    return [player.id for player in await queryset]


@pytest.mark.asyncio
async def test_descending_ordering_puts_nulls_first(clickhouse_db):
    await create_players()

    queryset = Player.objects.order_by("-rating", "id")
    assert await get_ids(queryset) == [4, 7, 5, 6, 3, 1, 2]
    assert await get_ids(queryset.reverse()) == [2, 1, 3, 6, 5, 7, 4]
    assert (await queryset.last()).id == 2
    assert await get_ids(Player.objects.order_by(F("rating").desc(nulls_last=True), "id")) == [5, 6, 3, 1, 2, 4, 7]


@pytest.mark.asyncio
async def test_pages_after_a_cursor_keep_the_null_rows(clickhouse_db):
    await create_players()

    queryset = Player.objects.order_by("-rating", "id")
    walked = []
    page = await queryset.limit(2)
    while page:
        walked.extend(player.id for player in page)
        page = await queryset.after_cursor(*queryset.cursor_values(page[-1])).limit(2)
    assert walked == [4, 7, 5, 6, 3, 1, 2]


@pytest.mark.asyncio
async def test_a_range_of_values_around_a_null_holds_its_peers(clickhouse_db):
    await create_players()

    async def get_sums(*ordering, start=-5, end=5):
        window = Window(Sum("rating"), order_by=list(ordering), frame=ValueRange(start=start, end=end))
        return await Player.objects.order_by("id").annotate(value=window).values_list("value", flat=True)

    assert await get_sums("rating") == [6.0, 6.0, 6.0, None, 26.0, 26.0, None]
    assert await get_sums("-rating") == [6.0, 6.0, 6.0, None, 26.0, 26.0, None]
    assert await get_sums(F("rating").asc(nulls_first=True)) == [6.0, 6.0, 6.0, None, 26.0, 26.0, None]
    assert await get_sums("rating", start=None) == [6.0, 6.0, 6.0, 32.0, 32.0, 32.0, 32.0]
    assert await get_sums("-rating", end=None) == [6.0, 6.0, 6.0, 32.0, 32.0, 32.0, 32.0]


@pytest.mark.asyncio
async def test_ordered_array_aggregate_places_nulls(clickhouse_db):
    await create_players()

    async def collect(*ordering):
        return (await Player.objects.aggregate(ids=ArrayAgg("id", order_by=list(ordering))))["ids"]

    assert await collect("rating", "id") == [1, 2, 3, 5, 6, 4, 7]
    assert await collect("-rating", "-id") == [7, 4, 6, 5, 3, 2, 1]
    assert await collect(F("rating").desc(nulls_last=True), F("id").desc()) == [6, 5, 3, 2, 1, 7, 4]
    assert await collect(F("rating").asc(nulls_first=True), "id") == [4, 7, 1, 2, 3, 5, 6]


@pytest.mark.parametrize("constant", [5, 1, True])
@pytest.mark.asyncio
async def test_an_ordering_by_a_constant_is_no_column_position(clickhouse_db, constant):
    """ClickHouse reads a number in ORDER BY as a column's position: Value(5) failed with "Positional
    argument number 5 is out of bounds", Value(1) silently ordered by the first column."""
    await Player.objects.create(id=1, name="b")
    await Player.objects.create(id=2, name="a")
    queryset = Player.objects.annotate(x=Value(constant)).order_by("x", "name")
    assert await queryset.values_list("id", flat=True) == [2, 1]
