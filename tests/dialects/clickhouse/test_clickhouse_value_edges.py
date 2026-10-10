"""Values at the edges of what ClickHouse stores and parses: dates outside its range are refused
instead of clamped, a statement holding many or large values runs, a sample deviation of one value is
NULL and a time of day reads as the text a time is stored as."""

import datetime

import pytest

from hare.exceptions import ValidationError
from hare.fields import TimeField
from hare.query.expressions import F
from hare.query.functions import Cast, StdDev, Variance
from tests.dialects.clickhouse.models import Player

EARLY = datetime.date(1850, 6, 1)
LATE = datetime.datetime(2400, 1, 1, tzinfo=datetime.UTC)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "values",
    [
        {"born": EARLY},
        {"born": datetime.date(2300, 1, 1)},
        {"joined": LATE},
        {"joined": datetime.datetime(1899, 12, 31, 23)},
    ],
)
async def test_a_date_clickhouse_doesnt_store_is_refused(clickhouse_db, values):
    with pytest.raises(ValidationError, match="1900-01-01 to 2299-12-31"):
        await Player.objects.create(id=1, name="early", **values)
    with pytest.raises(ValidationError, match="1900-01-01 to 2299-12-31"):
        await Player.objects.bulk_create([Player(id=2, name="ok"), Player(id=3, name="early", **values)])
    assert await Player.objects.count() == 0


@pytest.mark.asyncio
async def test_a_filter_by_a_date_clickhouse_doesnt_store_is_refused(clickhouse_db):
    with pytest.raises(ValidationError, match="1900-01-01 to 2299-12-31"):
        await Player.objects.filter(born__lt=EARLY)


@pytest.mark.asyncio
async def test_the_edges_of_the_range_are_stored(clickhouse_db):
    first = datetime.datetime(1900, 1, 1, tzinfo=datetime.UTC)
    last = datetime.datetime(2299, 12, 31, 23, 59, 59, 999999, tzinfo=datetime.UTC)
    await Player.objects.create(id=1, name="first", born=datetime.date(1900, 1, 1), joined=first)
    await Player.objects.bulk_create([Player(id=2, name="last", born=datetime.date(2299, 12, 31), joined=last)])
    assert await Player.objects.order_by("id").values_list("born", "joined") == [
        (datetime.date(1900, 1, 1), first),
        (datetime.date(2299, 12, 31), last),
    ]


@pytest.mark.asyncio
async def test_a_long_in_list_runs(clickhouse_db):
    await Player.objects.create(id=7, name="seven")
    # ~500 KB of SQL text, over the server's default 256 KiB.
    assert await Player.objects.filter(id__in=list(range(100_000))).values_list("id", flat=True) == [7]


@pytest.mark.asyncio
async def test_a_large_document_is_updated(clickhouse_db):
    player = await Player.objects.create(id=1, name="big")
    player.data = {"text": "x" * 400_000}
    await player.save()
    await player.refresh_from_db()
    assert len(player.data["text"]) == 400_000


@pytest.mark.asyncio
async def test_a_sample_deviation_of_one_value_is_none(clickhouse_db):
    await Player.objects.create(id=1, name="one", rating=4.5)
    assert await Player.objects.aggregate(
        deviation=StdDev("rating", sample=True), variance=Variance("rating", sample=True)
    ) == {"deviation": None, "variance": None}
    grouped = await Player.objects.values("name").annotate(deviation=StdDev("rating", sample=True))
    assert grouped == [{"name": "one", "deviation": None}]
    await Player.objects.create(id=2, name="two", rating=6.5)
    result = await Player.objects.aggregate(deviation=StdDev("rating", sample=True))
    assert result["deviation"] == pytest.approx(2**0.5)


@pytest.mark.asyncio
async def test_a_time_of_day_reads_as_a_stored_time(clickhouse_db):
    await Player.objects.create(id=1, name="whole", joined=datetime.datetime(2024, 1, 2, 3, 4, 5, tzinfo=datetime.UTC))
    await Player.objects.create(
        id=2, name="fraction", joined=datetime.datetime(2024, 1, 2, 3, 4, 5, 678901, tzinfo=datetime.UTC)
    )
    assert await Player.objects.filter(joined__time=datetime.time(3, 4, 5)).values_list("id", flat=True) == [1]
    assert await Player.objects.filter(joined__time__gt=datetime.time(3, 4, 5)).values_list("id", flat=True) == [2]
    times = (
        await Player.objects.annotate(clock=Cast(F("joined"), TimeField()))
        .order_by("id")
        .values_list("clock", flat=True)
    )
    # A time of day is aware under use_timezone, as a TimeField's own value is.
    assert times == [
        datetime.time(3, 4, 5, tzinfo=datetime.UTC),
        datetime.time(3, 4, 5, 678901, tzinfo=datetime.UTC),
    ]
