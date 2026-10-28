"""Cast() as ClickHouse converts - its own conversions, hare checks nothing: a fraction cut off an integer
and a Decimal's places, a value out of an integer type wrapped around, bad text NULL in a Nullable type,
a text moment read by parseDateTime64BestEffort(); a NULL row stays NULL."""

import datetime
import decimal

import pytest

from hare import fields
from hare.dialects.clickhouse.fields import ClickhouseDatetimeField
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
        decimal.Decimal("2"),
        decimal.Decimal("-2"),
        decimal.Decimal("0"),
    ]
    assert await get_values(Cast("rating", fields.DecimalField(max_digits=5, decimal_places=0))) == [
        decimal.Decimal("2"),
        decimal.Decimal("-1"),
        None,
    ]


@pytest.mark.asyncio
async def test_a_float_converts_to_a_decimal_as_clickhouse_converts(clickhouse_db):
    """By the double itself, its places cut off - 2.675 is 2.67499..."""
    await create_players()

    for value, places, expected in ((2.675, 2, "2.67"), (0.125, 2, "0.12"), (-0.125, 2, "-0.12"), (0.0, 1, "0.0")):
        target = fields.DecimalField(max_digits=30, decimal_places=places)
        assert await get_values(Cast(Value(value), target), id=1) == [decimal.Decimal(expected)]


@pytest.mark.asyncio
async def test_a_value_out_of_the_target_is_converted_as_clickhouse_converts(clickhouse_db):
    await create_players()

    assert await get_values(Cast(Value(3e9), fields.IntField()), id=1) == [-(2**31)]
    assert await get_values(Cast(Value(40000.0), fields.SmallIntField()), id=1) == [40000 - 2**16]
    assert await get_values(Cast(Value(9999.95), fields.DecimalField(max_digits=5, decimal_places=1)), id=1) == [
        decimal.Decimal("9999.9")
    ]


@pytest.mark.asyncio
async def test_text_is_read_as_clickhouse_reads_it(clickhouse_db):
    await create_players()

    assert await get_values(Cast(Value("42"), fields.IntField()), id=1) == [42]
    assert await get_values(Cast(Value("-1.25"), fields.DecimalField(max_digits=5, decimal_places=1)), id=1) == [
        decimal.Decimal("-1.2")
    ]
    assert await get_values(Cast(Value("1.5e2"), fields.DecimalField(max_digits=5, decimal_places=1)), id=1) == [
        decimal.Decimal("150.0")
    ]
    assert await get_values(Cast(Value("1.5"), fields.FloatField()), id=1) == [1.5]
    for text, expected in (("true", True), ("0", False), ("YES", True), ("N", False)):
        assert await get_values(Cast(Value(text), fields.BooleanField()), id=1) == [expected]


@pytest.mark.asyncio
async def test_bad_text_is_null_as_clickhouse_converts_it_into_a_nullable_type(clickhouse_db):
    """ClickHouse reads no number padded with spaces either."""
    await create_players()

    for expression in (
        Cast("name", fields.IntField()),
        Cast("name", fields.DecimalField(max_digits=5, decimal_places=1)),
        Cast("name", fields.FloatField()),
    ):
        assert await get_values(expression) == [None, None, None]
    for expression in (Cast(Value("4.2"), fields.IntField()), Cast(Value(""), fields.IntField())):
        assert await get_values(expression, id=1) == [None]
    for expression in (Cast(Value("o"), fields.BooleanField()), Cast(Value("maybe"), fields.BooleanField())):
        with pytest.raises(OperationalError):
            await get_values(expression, id=1)


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
async def test_a_text_date_is_read_as_clickhouse_reads_it(clickhouse_db, target):
    """parseDateTime64BestEffort() - a date it lacks a part of completed, a clock past its day rolled
    over, a year past the ones it holds read as the nearest day it holds."""
    await create_players()

    cases = [
        ("2024/01/12", datetime.date(2024, 1, 12)),
        ("20240112", datetime.date(2024, 1, 12)),
        ("Jan 12 2024", datetime.date(2024, 1, 12)),
        ("2024-01", datetime.date(2024, 1, 1)),
        ("2024-01-12 25:00:00", datetime.date(2024, 1, 13)),
        ("1900-01-01", datetime.date(1900, 1, 1)),
        ("2024-01-12 23:59:59+05:30", datetime.date(2024, 1, 12)),
    ]
    # ClickHouse 26.7 widened DateTime64 to the years 1 to 9999 - such a year is no longer moved to the nearest one.
    if (await Player.get_connection().get_server_version())[:2] < (26, 7):
        cases += [("9999-12-31", datetime.date(2299, 12, 31)), ("1899-12-31", datetime.date(1900, 1, 1))]
    for text, day in cases:
        assert [
            value if target is fields.DateField else value.date()
            for value in await get_values(Cast(Value(text), target()), id=1)
        ] == [day]


@pytest.mark.asyncio
async def test_a_text_time_of_day_is_read_as_clickhouse_reads_it(clickhouse_db):
    """After the first day of 1970 - a clock past its day rolled over."""
    await create_players()

    for text, expected in (
        (" 7:05 ", datetime.time(7, 5, tzinfo=datetime.UTC)),
        ("25:00", datetime.time(1, 0, tzinfo=datetime.UTC)),
        ("12:60", datetime.time(13, 0, tzinfo=datetime.UTC)),
        (None, None),
    ):
        assert await get_values(Cast(Value(text), fields.TimeField()), id=1) == [expected]
    with pytest.raises(OperationalError):
        await get_values(Cast(Value("abc"), fields.TimeField()), id=1)


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


@pytest.mark.parametrize("precision", [None, 0, 3, 6])
@pytest.mark.asyncio
async def test_a_moment_cast_to_a_datetime_field_takes_its_precision(clickhouse_db, precision):
    """A cast to a datetime was written as DateTime64(6) whatever the target field - a second-precision one
    kept the microseconds a native CAST to its type drops."""
    moment = datetime.datetime(2025, 1, 1, 0, 0, 0, 123456, tzinfo=datetime.UTC)
    await Player.objects.create(id=1, name="p1", joined=moment)
    column_type = "DateTime('UTC')" if precision is None else f"DateTime64({precision}, 'UTC')"
    _, rows = await Player.get_connection().execute(
        f'SELECT CAST("joined" AS {column_type}) AS native FROM "player"', returns_rows=True
    )
    native = rows[0]["native"].replace(tzinfo=datetime.UTC)
    converted = (
        await Player.objects.annotate(
            converted=Cast("joined", ClickhouseDatetimeField(precision=precision))
        ).values_list("converted", flat=True)
    )[0]
    assert converted == native
