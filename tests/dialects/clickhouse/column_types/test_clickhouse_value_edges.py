"""Values at the edges of what ClickHouse stores and parses: dates outside its range are refused
instead of clamped, a statement holding many or large values runs, a sample deviation of one value is
NULL, a time of day reads as the text a time is stored as, and a value cast to text is PostgreSQL's
text of it."""

import datetime
import decimal

import pytest

from hare.exceptions import ValidationError
from hare.fields import CharField, TextField, TimeField
from hare.query.expressions import F, Value
from hare.query.functions import Cast, Concat, StdDev, Variance
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("values", "texts"),
    [
        (
            {
                "joined": datetime.datetime(2024, 1, 2, 3, 4, 5, 678900, tzinfo=datetime.UTC),
                "score": decimal.Decimal("12.5"),
                "rating": 1.5e-7,
                "born": datetime.date(1950, 6, 1),
                "active": False,
            },
            ("2024-01-02 03:04:05.678900", "12.5", "1.5e-7", "1950-06-01", "false"),
        ),
        (
            {"joined": datetime.datetime(2024, 1, 2, 3, 4, 5, tzinfo=datetime.UTC), "rating": 1e308},
            ("2024-01-02 03:04:05.000000", "0", "1e308", None, "true"),
        ),
        ({"rating": float("-inf")}, (None, "0", "-inf", None, "true")),
        ({"rating": 123456789012345678.0}, (None, "0", "123456789012345680", None, "true")),
        ({"rating": -0.0}, (None, "0", "-0", None, "true")),
        ({"rating": 0.0001}, (None, "0", "0.0001", None, "true")),
    ],
)
async def test_a_value_cast_to_text_is_clickhouse_text_of_it(clickhouse_db, values, texts):
    await Player.objects.create(id=1, name="cast", **values)
    text_field = TextField()
    assert (
        await Player.objects.annotate(
            joined_text=Cast("joined", text_field),
            score_text=Cast("score", text_field),
            rating_text=Cast("rating", text_field),
            born_text=Cast("born", text_field),
            active_text=Cast("active", text_field),
        ).values_list("joined_text", "score_text", "rating_text", "born_text", "active_text")
    ) == [texts]


@pytest.mark.asyncio
async def test_a_text_cast_is_the_whole_text(clickhouse_db):
    """ClickHouse's String has no length - a CharField's max_length cuts nothing."""
    await Player.objects.create(id=123456, name="abcdef")
    assert await Player.objects.annotate(
        name_text=Cast("name", CharField(max_length=3)), id_text=Cast("id", CharField(max_length=2))
    ).values_list("name_text", "id_text") == [("abcdef", "123456")]


EXACT_FLOATS = [123456789012345678.0, 5e-324, 2.2250738585072014e-308, 1.7976931348623157e308, 0.1, -2.675, 1e23]


@pytest.mark.asyncio
async def test_a_float_is_written_exactly(clickhouse_db):
    """The text of an INSERT's VALUES and of a mutation is read by a parser off in the last digits,
    and 5e-324 is no literal of ClickHouse's - a float is written as its bytes."""
    for index, value in enumerate(EXACT_FLOATS):
        await Player.objects.create(id=index, name="created", rating=value)
    assert await Player.objects.values_list("rating", flat=True) == EXACT_FLOATS
    for index, value in enumerate(reversed(EXACT_FLOATS)):
        await Player.objects.filter(id=index).update(rating=value)
    assert await Player.objects.values_list("rating", flat=True) == list(reversed(EXACT_FLOATS))
    for value in EXACT_FLOATS:
        assert await Player.objects.filter(rating=value).count() == 1


@pytest.mark.asyncio
async def test_a_decimal_reads_with_its_scale(clickhouse_db):
    await Player.objects.create(id=1, name="scaled", score=decimal.Decimal("10.50"))
    await Player.objects.filter(id=1).update(score=F("score") * 2)

    assert [str(score) for score in await Player.objects.values_list("score", flat=True)] == ["21.00"]
    assert str((await Player.objects.get(id=1)).score) == "21.00"


class Opaque:
    def __str__(self) -> str:
        return "OPAQUE"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value", [{1}, range(3), Opaque(), int, 1j], ids=["set", "range", "object", "class", "complex"]
)
async def test_a_value_no_clickhouse_type_holds_is_refused(clickhouse_db, value):
    """ClickHouse wrote the text of such a value - into a result, and through an update into the table -
    where PostgreSQL and SQLite refuse it."""
    await Player.objects.create(id=1, name="a")
    with pytest.raises(ValidationError, match="which no ClickHouse value holds"):
        await Player.objects.annotate(value=Concat("name", Value(value))).values_list("value", flat=True)
    with pytest.raises(ValidationError, match="which no ClickHouse value holds"):
        await Player.objects.filter(id=1).update(name=Concat("name", Value(value)))
    assert await Player.objects.values_list("name", flat=True) == ["a"]
