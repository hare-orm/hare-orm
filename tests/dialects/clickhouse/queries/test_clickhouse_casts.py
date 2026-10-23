"""Cast() as ClickHouse converts - a fraction cut off an integer - refusing what its own conversions
would silently turn into another value: a value out of the target's range or precision, bad text even
in a Nullable column, an impossible date (not rolled over); a NULL row stays NULL, a Decimal rounds half
away from zero to its places, a float converts to one by its shortest digits."""

import datetime
import decimal

import pytest

from hare import fields
from hare.exceptions import OperationalError
from hare.query.expressions import F, Value
from hare.query.functions import Cast, Concat, Upper
from tests.dialects.clickhouse.models import Player, Revision


async def get_values(expression, **filters):
    return (
        await Player.objects.filter(**filters)
        .order_by("id")
        .annotate(value=expression)
        .values_list("value", flat=True)
    )


async def create_players():
    await Player.objects.bulk_create(
        [
            Player(
                id=1,
                name=" 42 ",
                score=decimal.Decimal("2.50"),
                rating=2.5,
                born=datetime.date(2024, 2, 29),
                joined=datetime.datetime(2024, 2, 29, 12, tzinfo=datetime.UTC),
            ),
            Player(id=2, name="abc", score=decimal.Decimal("-2.50"), rating=-1.5, active=False),
            Player(id=3, name="null", score=decimal.Decimal("0"), rating=None),
        ]
    )


@pytest.mark.asyncio
async def test_a_number_converts_as_clickhouse_converts(clickhouse_db):
    await create_players()

    assert await get_values(Cast("rating", fields.IntField())) == [2, -1, None]
    assert await get_values(Cast(F("rating") * 3, fields.BigIntField())) == [7, -4, None]
    assert await get_values(Cast("score", fields.IntField())) == [2, -2, 0]
    assert await get_values(Cast("score", fields.DecimalField(max_digits=5, decimal_places=0))) == [
        decimal.Decimal("3"),
        decimal.Decimal("-3"),
        decimal.Decimal("0"),
    ]
    assert await get_values(Cast("rating", fields.DecimalField(max_digits=5, decimal_places=0))) == [
        decimal.Decimal("3"),
        decimal.Decimal("-2"),
        None,
    ]


@pytest.mark.asyncio
async def test_a_float_converts_to_a_decimal_by_its_shortest_digits(clickhouse_db):
    await create_players()

    for value, places, expected in (
        (2.675, 2, "2.68"),
        (0.125, 2, "0.13"),
        (-0.125, 2, "-0.13"),
        (0.1 + 0.2, 20, "0.30000000000000004000"),
        (1e-7, 10, "1E-7"),
        (1e20, 2, "100000000000000000000.00"),
        (0.0, 1, "0.0"),
    ):
        target = fields.DecimalField(max_digits=30, decimal_places=places)
        assert await get_values(Cast(Value(value), target), id=1) == [decimal.Decimal(expected)]


@pytest.mark.asyncio
async def test_a_value_out_of_the_target_is_refused(clickhouse_db):
    await create_players()

    for expression, message in (
        (Cast(Value(3e9), fields.IntField()), "cannot be safely converted"),
        (Cast(Value(40000.0), fields.SmallIntField()), "cannot be safely converted"),
        (Cast(F("rating") * 1e10, fields.IntField()), "cannot be safely converted"),
        (Cast(Value(decimal.Decimal("123456.5")), fields.DecimalField(max_digits=5, decimal_places=1)), "digits"),
        (Cast(Value(9999.95), fields.DecimalField(max_digits=5, decimal_places=1)), "more digits"),
        (Cast(F("score") * 10000, fields.DecimalField(max_digits=5, decimal_places=2)), "more digits"),
    ):
        with pytest.raises(OperationalError, match=message):
            await get_values(expression)
    assert await get_values(Cast(Value(9999.94), fields.DecimalField(max_digits=5, decimal_places=1)), id=1) == [
        decimal.Decimal("9999.9")
    ]


@pytest.mark.asyncio
async def test_text_is_read_as_clickhouse_reads_it(clickhouse_db):
    await create_players()

    assert await get_values(Cast(Value("42"), fields.IntField()), id=1) == [42]
    assert await get_values(Cast(Value("-1.25"), fields.DecimalField(max_digits=5, decimal_places=1)), id=1) == [
        decimal.Decimal("-1.3")
    ]
    assert await get_values(Cast(Value("1.5e2"), fields.DecimalField(max_digits=5, decimal_places=1)), id=1) == [
        decimal.Decimal("150.0")
    ]
    assert await get_values(Cast(Value("1.5"), fields.FloatField()), id=1) == [1.5]
    for text, expected in (("true", True), ("0", False), ("YES", True), ("N", False)):
        assert await get_values(Cast(Value(text), fields.BooleanField()), id=1) == [expected]
    # ClickHouse reads no number padded with spaces.
    with pytest.raises(OperationalError):
        await get_values(Cast("name", fields.IntField()), id=1)


@pytest.mark.asyncio
async def test_bad_text_is_refused_also_in_a_nullable_column(clickhouse_db):
    await create_players()

    for expression in (
        Cast("name", fields.IntField()),
        Cast("name", fields.DecimalField(max_digits=5, decimal_places=1)),
        Cast("name", fields.FloatField()),
        Cast(Value("4.2"), fields.IntField()),
        Cast(Value(""), fields.IntField()),
        Cast(Value("o"), fields.BooleanField()),
        Cast(Value("maybe"), fields.BooleanField()),
    ):
        with pytest.raises(OperationalError):
            await get_values(expression, id__in=[2, 3])


@pytest.mark.asyncio
async def test_a_text_date_is_read_as_written_and_an_impossible_one_refused(clickhouse_db):
    await create_players()

    assert await get_values(Cast(Value("2024-02-29 23:30:00-02:00"), fields.DateField()), id=1) == [
        datetime.date(2024, 2, 29)
    ]
    assert await get_values(Cast(Value("2024-02-29 23:30:00-02:00"), fields.DatetimeField()), id=1) == [
        datetime.datetime(2024, 3, 1, 1, 30, tzinfo=datetime.UTC)
    ]
    assert await get_values(Cast(Value(" 2024-02-29 "), fields.DateField()), id=1) == [datetime.date(2024, 2, 29)]
    for text in ("2023-02-29", "2024-02-30", "2024-13-01"):
        with pytest.raises(OperationalError):
            await get_values(Cast(Value(text), fields.DateField()), id=1)


@pytest.mark.parametrize("target", [fields.DateField, fields.DatetimeField])
@pytest.mark.asyncio
async def test_a_text_naming_no_whole_date_is_refused(clickhouse_db, target):
    """ClickHouse completed it with the current year, month or day - "12" was the 12th of January."""
    await create_players()

    for text in ("12", "2.5", "10:20", "2024", "2024-01"):
        with pytest.raises(OperationalError):
            await get_values(Cast(Value(text), target()), id=1)
    for text in ("2024/01/12", "20240112", "Jan 12 2024"):
        assert [
            value if target is fields.DateField else value.date()
            for value in await get_values(Cast(Value(text), target()), id=1)
        ] == [datetime.date(2024, 1, 12)]


@pytest.mark.parametrize("target", [fields.DateField, fields.DatetimeField])
@pytest.mark.asyncio
async def test_an_impossible_clock_or_a_date_clickhouse_cant_hold_is_refused(clickhouse_db, target):
    """ClickHouse rolled "25:00" over into the next day and read "9999-12-31" as 2299-12-31."""
    await create_players()

    for text in (
        "2024-01-12 25:00:00",
        "2024-01-12 24:00:00",
        "2024-01-12 12:60:00",
        "2024-01-12 12:30:60",
        "9999-12-31",
        "1899-12-31",
    ):
        with pytest.raises(OperationalError):
            await get_values(Cast(Value(text), target()), id=1)
    for text, day in (
        ("1900-01-01", datetime.date(1900, 1, 1)),
        ("2299-12-31", datetime.date(2299, 12, 31)),
        ("2024-01-12 23:59:59+05:30", datetime.date(2024, 1, 12)),
    ):
        assert [
            value if target is fields.DateField else value.date()
            for value in await get_values(Cast(Value(text), target()), id=1)
        ] == [day]


@pytest.mark.asyncio
async def test_a_text_naming_no_time_of_day_is_refused(clickhouse_db):
    """Hare read the text after the first day of 1970 - an empty one was midnight, "-1" one o'clock."""
    await create_players()

    for text in ("", " ", "-1", "abc", "25:00", "12:60", "2024-01-12"):
        with pytest.raises(OperationalError):
            await get_values(Cast(Value(text), fields.TimeField()), id=1)
    assert await get_values(Cast(Value(" 7:05 "), fields.TimeField()), id=1) == [
        datetime.time(7, 5, tzinfo=datetime.UTC)
    ]
    assert await get_values(Cast(Value(None), fields.TimeField()), id=1) == [None]


@pytest.mark.asyncio
async def test_a_null_row_stays_null(clickhouse_db):
    await create_players()

    assert await get_values(Cast("rating", fields.FloatField())) == [2.5, -1.5, None]
    assert await get_values(Cast("rating", fields.DecimalField(max_digits=5, decimal_places=1))) == [
        decimal.Decimal("2.5"),
        decimal.Decimal("-1.5"),
        None,
    ]
    assert await get_values(Cast("born", fields.DatetimeField())) == [
        datetime.datetime(2024, 2, 29, tzinfo=datetime.UTC),
        None,
        None,
    ]
    assert await get_values(Cast("born", fields.TextField())) == ["2024-02-29", None, None]


@pytest.mark.asyncio
async def test_json_text_is_the_text_clickhouse_writes(clickhouse_db):
    await Player.objects.bulk_create(
        [Player(id=1, name="a", data={"a": [1, 2], "b": {"c": "x, y: z"}}), Player(id=2, name="b")]
    )

    text = '{"a":[1,2],"b":{"c":"x, y: z"}}'
    assert await get_values(Cast("data", fields.TextField())) == [text, None]
    assert await get_values(Concat("data", Value("|"))) == [f"{text}|", "|"]


@pytest.mark.asyncio
async def test_a_boolean_and_a_number_convert_into_each_other(clickhouse_db):
    await create_players()

    assert await get_values(Cast("active", fields.IntField())) == [1, 0, 1]
    assert await get_values(Cast("active", fields.BigIntField())) == [1, 0, 1]
    assert await get_values(Cast("active", fields.FloatField())) == [1.0, 0.0, 1.0]
    assert await get_values(Cast("rating", fields.BooleanField())) == [True, True, None]


@pytest.mark.asyncio
async def test_a_cast_over_a_composite_key_model(clickhouse_db):
    await Revision.objects.bulk_create(
        [
            Revision(document_id=1, number=1, title="2024-02-30", score=2.5),
            Revision(document_id=1, number=2, title="7", score=None),
        ]
    )

    revisions = Revision.objects.order_by("number").annotate(
        whole=Cast("score", fields.IntField()), number_text=Cast("number", fields.TextField())
    )
    assert await revisions.values_list("whole", "number_text") == [(2, "1"), (None, "2")]
    assert await Revision.objects.filter(pk=(1, 2)).annotate(value=Cast("title", fields.IntField())).values_list(
        "value", flat=True
    ) == [7]
    with pytest.raises(OperationalError):
        await (
            Revision.objects.filter(pk=(1, 1))
            .annotate(value=Cast("title", fields.DateField()))
            .values_list("value", flat=True)
        )


@pytest.mark.asyncio
async def test_cast_to_a_time_of_day_reads_its_text(clickhouse_db):
    """A time of day is kept as its text - the cast formatted it as a moment, which ClickHouse refuses of a
    String (ILLEGAL_TYPE_OF_ARGUMENT)."""
    await Player.objects.create(id=1, name="10:20:30")
    time_field = fields.TimeField()
    assert await get_values(Cast("name", time_field)) == [datetime.time(10, 20, 30, tzinfo=datetime.UTC)]
    assert await get_values(Cast(Cast("name", time_field), fields.TimeField())) == [
        datetime.time(10, 20, 30, tzinfo=datetime.UTC)
    ]


@pytest.mark.parametrize(
    "source", [Value("10:20:30"), Upper("name"), Concat("name", Value(""))], ids=["literal", "upper", "concat"]
)
@pytest.mark.asyncio
async def test_cast_of_a_text_expression_to_a_time_of_day_reads_its_text(clickhouse_db, source):
    """Only a text column was read as one - a text expression of no field was formatted as a moment."""
    await Player.objects.create(id=1, name="10:20:30")
    assert await get_values(Cast(source, fields.TimeField())) == [datetime.time(10, 20, 30, tzinfo=datetime.UTC)]
