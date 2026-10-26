"""PostgreSQL's types of a wall clock without a time zone, of an interval and of a fixed-width text -
``timestamp``, ``time``, ``interval``, ``character(n)`` - each kept by a field of its own."""

from __future__ import annotations

import datetime
import os

import pytest

from hare.contrib.test import hare_test_context
from hare.core.connections.connections import Connections
from hare.exceptions import ValidationError
from hare.inspectdb.introspection.database_catalog import DatabaseCatalog
from hare.query.functions import ExtractHour, ExtractYear, Trunc, TruncDate, TruncHour, TruncMonth, TruncTime
from tests.dialects.postgresql.column_types.models_wall_clock_types import WallClockEntry
from tests.dialects.postgresql.conftest import skip_if_not_postgres

MODULE = "tests.dialects.postgresql.column_types.models_wall_clock_types"


def wall_clock_context():
    skip_if_not_postgres()
    return hare_test_context(
        modules=[MODULE], db_url=os.environ["HARE_TEST_DB"], app_label="models", connection_label="models"
    )


@pytest.mark.asyncio
async def test_a_wall_clock_an_interval_and_a_fixed_width_text_are_kept_as_written():
    """A naive timestamp, a time, an interval and a character(n) column had no field."""
    async with wall_clock_context():
        at = datetime.datetime(2020, 1, 2, 3, 4, 5, 678901)
        await WallClockEntry.objects.create(
            id=1, at=at, opens=datetime.time(9, 30), lasts=datetime.timedelta(days=2, seconds=5), code="ab"
        )
        entry = await WallClockEntry.objects.get(id=1)
        assert (entry.at, entry.opens, entry.lasts, entry.code) == (
            at,
            datetime.time(9, 30),
            datetime.timedelta(days=2, seconds=5),
            "ab  ",
        )
        assert await WallClockEntry.objects.filter(at__year=2020, at__hour=3, opens__hour=9).count() == 1
        assert await WallClockEntry.objects.filter(lasts__gt=datetime.timedelta(days=1), code="ab").count() == 1
        columns = await Connections.get("models").execute_dicts(
            "SELECT column_name, data_type, character_maximum_length FROM information_schema.columns "
            "WHERE table_name = 'wallclockentry' AND column_name <> 'id' ORDER BY ordinal_position"
        )
        assert [(column["column_name"], column["data_type"]) for column in columns] == [
            ("at", "timestamp without time zone"),
            ("opens", "time without time zone"),
            ("lasts", "interval"),
            ("code", "character"),
        ]


@pytest.mark.asyncio
async def test_date_functions_read_the_wall_clock_as_it_is():
    """Extract(), Trunc(), __date/__time and values("at__date") refused a naive timestamp and a time
    without a zone - only the date parts were read."""
    async with wall_clock_context():
        at = datetime.datetime(2020, 1, 2, 23, 30, 15)
        await WallClockEntry.objects.create(id=1, at=at, opens=datetime.time(9, 45, 10))
        rows = await WallClockEntry.objects.annotate(
            year=ExtractYear("at"),
            hour=ExtractHour("at"),
            month=TruncMonth("at"),
            hour_start=TruncHour("at"),
            day=TruncDate("at"),
            clock=TruncTime("at"),
            opens_hour=ExtractHour("opens"),
            opens_start=Trunc("opens", "hour"),
        ).values("year", "hour", "month", "hour_start", "day", "clock", "opens_hour", "opens_start")
        assert rows == [
            {
                "year": 2020,
                "hour": 23,
                "month": datetime.datetime(2020, 1, 1),
                "hour_start": datetime.datetime(2020, 1, 2, 23),
                "day": datetime.date(2020, 1, 2),
                "clock": datetime.time(23, 30, 15),
                "opens_hour": 9,
                "opens_start": datetime.time(9),
            }
        ]
        assert await WallClockEntry.objects.values_list("at__date", "at__time", "at__month", "opens__minute") == [
            (datetime.date(2020, 1, 2), datetime.time(23, 30, 15), 1, 45)
        ]
        assert (
            await WallClockEntry.objects.filter(
                at__date=datetime.date(2020, 1, 2), at__time__gt=datetime.time(23)
            ).count()
            == 1
        )
        assert await WallClockEntry.objects.filter(at__date=datetime.date(2020, 1, 3)).count() == 0


@pytest.mark.asyncio
async def test_a_value_with_a_time_zone_is_refused():
    async with wall_clock_context():
        with pytest.raises(ValidationError, match="holds a time zone"):
            await WallClockEntry.objects.create(id=1, at=datetime.datetime(2020, 1, 2, tzinfo=datetime.UTC))
        with pytest.raises(ValidationError, match="holds a time zone"):
            await WallClockEntry.objects.create(id=1, opens=datetime.time(9, tzinfo=datetime.UTC))
        with pytest.raises(ValidationError):
            await WallClockEntry.objects.create(id=1, code="abcde")


@pytest.mark.asyncio
async def test_the_end_of_a_day_is_no_time_of_day():
    """The text 24:00 was stored as 00:00 - Python 3.14 reads it as midnight at the start of the day."""
    async with wall_clock_context():
        with pytest.raises(ValidationError, match="end of a day"):
            await WallClockEntry.objects.create(id=1, opens="24:00:00")
        with pytest.raises(ValidationError, match="end of a day"):
            await WallClockEntry.objects.filter(opens=" 24:00").count()
        assert await WallClockEntry.objects.count() == 0


@pytest.mark.asyncio
async def test_the_columns_map_back_onto_their_fields():
    """inspectdb read these columns as a TextField with a TODO."""
    async with wall_clock_context():
        connection = Connections.get("models")
        table = await DatabaseCatalog.inspect_table(connection, "wallclockentry")
        introspector = DatabaseCatalog.get_introspector_class(connection)
        mapped = {column.name: introspector.map_column_type(column) for column in table.columns if column.name != "id"}
        assert mapped == {
            "at": ("hare.dialects.postgresql.fields.NaiveDatetimeField", {}, False),
            "opens": ("hare.dialects.postgresql.fields.NaiveTimeField", {}, False),
            "lasts": ("hare.dialects.postgresql.fields.IntervalField", {}, False),
            "code": ("hare.dialects.postgresql.fields.FixedCharField", {"max_length": 4}, False),
        }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "at",
    [datetime.datetime(1969, 12, 31, 23, 59, 59), datetime.datetime(1950, 6, 1, 12), datetime.datetime(1900, 1, 1)],
)
async def test_a_wall_clock_before_1970_is_kept_as_written(at):
    """asyncpg on Windows got the system zone attached to such a value (for a timestamptz) - the
    timestamp column took it as that zone's time and stored it shifted to UTC."""
    async with wall_clock_context():
        await WallClockEntry.objects.create(id=1, at=at)
        assert (await WallClockEntry.objects.get(id=1)).at == at
        rows = await Connections.get("models").execute_dicts("SELECT at::text AS at FROM wallclockentry")
        assert rows == [{"at": at.isoformat(sep=" ")}]
        assert await WallClockEntry.objects.filter(at=at).count() == 1


@pytest.mark.requires("asyncpg")
@pytest.mark.database_independent
def test_the_timestamp_codec_drops_the_zone_given_to_a_value_before_1970():
    """The zone the asyncpg client gives a naive value before 1970 isn't the value's - the codec of a
    timestamp column writes the wall clock it was given."""
    from hare.dialects.postgresql.drivers.asyncpg.asyncpg_timestamp_codec import AsyncpgTimestampCodec
    from hare.dialects.postgresql.drivers.asyncpg.pre_epoch_local_zone import PreEpochLocalZone

    at = datetime.datetime(1950, 6, 1, 12)
    given_zone = at.replace(tzinfo=PreEpochLocalZone(datetime.timedelta(hours=3)))
    assert AsyncpgTimestampCodec.encode(given_zone) == AsyncpgTimestampCodec.encode(at)


@pytest.mark.asyncio
async def test_a_wall_clock_is_shifted_and_subtracted():
    """F("at") + timedelta raised FieldError naming the wall clock "a non-date/time value"."""
    from hare.exceptions import FieldError
    from hare.query.expressions import F

    async with wall_clock_context():
        at = datetime.datetime(2026, 1, 1, 10)
        await WallClockEntry.objects.create(id=1, at=at)
        shift = datetime.timedelta(days=1, hours=2)
        rows = WallClockEntry.objects.annotate(later=F("at") + shift, earlier=F("at") - shift, none=F("at") - F("at"))
        assert await rows.values_list("later", "earlier", "none") == [(at + shift, at - shift, datetime.timedelta(0))]
        assert await WallClockEntry.objects.filter(at__lt=F("at") + shift).count() == 1
        assert await WallClockEntry.objects.filter(id=1).update(at=F("at") + shift) == 1
        assert (await WallClockEntry.objects.get(id=1)).at == at + shift
        with pytest.raises(FieldError, match="a wall clock in no zone"):
            moment = at.replace(tzinfo=datetime.UTC)
            await WallClockEntry.objects.annotate(gap=F("at") - moment).values_list("gap", flat=True)


@pytest.mark.asyncio
async def test_a_wall_clock_and_a_moment_arent_assigned_to_each_other():
    """update(at=F("moment")) went through the session's zone - another zone, another wall clock."""
    from hare.exceptions import FieldError
    from hare.query.expressions import F
    from tests.dialects.postgresql.column_types.models_wall_clock_types import WallClockMoment

    async with wall_clock_context():
        await WallClockMoment.objects.create(
            id=1, at=datetime.datetime(2026, 1, 1, 12), moment=datetime.datetime(2026, 1, 1, 12, tzinfo=datetime.UTC)
        )
        with pytest.raises(FieldError, match="the other sort of datetime"):
            await WallClockMoment.objects.update(at=F("moment"))
        with pytest.raises(FieldError, match="the other sort of datetime"):
            await WallClockMoment.objects.update(moment=F("at") + datetime.timedelta(hours=1))
        assert await WallClockMoment.objects.update(at=F("at") + datetime.timedelta(hours=1)) == 1


@pytest.mark.asyncio
async def test_a_wall_clock_and_a_moment_dont_make_one_value():
    """Coalesce(at, moment) and Greatest(at, moment) wrote a moment as its UTC wall clock - the zone the
    database read it in."""
    from hare.exceptions import FieldError
    from hare.query.expressions import F
    from hare.query.functions import Coalesce, Greatest
    from tests.dialects.postgresql.column_types.models_wall_clock_types import WallClockMoment

    async with wall_clock_context():
        at = datetime.datetime(2026, 1, 1, 12)
        await WallClockMoment.objects.create(id=1, moment=at.replace(tzinfo=datetime.UTC))
        for mixed in (
            Coalesce(F("at"), F("moment")),
            Greatest(F("at"), F("moment")),
            Coalesce(F("at"), at.replace(tzinfo=datetime.UTC)),
        ):
            with pytest.raises(FieldError, match="a wall clock in no zone"):
                await WallClockMoment.objects.update(at=mixed)
            with pytest.raises(FieldError, match="a wall clock in no zone"):
                await WallClockMoment.objects.annotate(value=mixed).values_list("value", flat=True)
        for literal in (at, at + datetime.timedelta(hours=5)):
            # A second literal is bound by the plan the first one built.
            values = await WallClockMoment.objects.annotate(value=Coalesce(F("at"), literal)).values_list(
                "value", flat=True
            )
            assert values == [literal]


@pytest.mark.asyncio
@pytest.mark.parametrize("zone", ["Asia/Tokyo", "America/New_York"])
async def test_a_naive_literal_beside_a_wall_clock_is_that_wall_clock(zone):
    """A naive datetime given to Coalesce, Greatest, Least, NullIf or Case beside a NaiveDatetimeField was
    bound as a moment of the configured zone - shifted by it, or refused on reading."""
    from hare.query.expressions import Case, F, Value, When
    from hare.query.functions import Coalesce, Greatest, Least, NullIf
    from hare.time.timezone import Timezone

    literal = datetime.datetime(2026, 7, 5, 9, 15)
    with Timezone.override(zone):
        async with wall_clock_context():
            await WallClockEntry.objects.create(id=1, at=None)
            expressions = {
                "coalesce": (Coalesce(F("at"), Value(literal)), literal),
                "greatest": (Greatest(F("at"), Value(literal)), literal),
                "least": (Least(F("at"), literal), literal),
                "nullif": (NullIf(F("at"), literal), None),
                "case": (Case(When(id=1, then=literal), default=F("at")), literal),
            }
            for name, (expression, expected) in expressions.items():
                values = await WallClockEntry.objects.annotate(value=expression).values_list("value", flat=True)
                assert values == [expected], name
            assert await WallClockEntry.objects.update(at=Coalesce(F("at"), Value(literal))) == 1
            assert (await WallClockEntry.objects.get(id=1)).at == literal


@pytest.mark.asyncio
async def test_interval_arithmetic_a_duration_has_no_result_of_is_refused():
    """A number added to an interval, a product of two intervals reached PostgreSQL (or the driver), which
    refused them with its own error."""
    from decimal import Decimal

    from hare.exceptions import FieldError
    from hare.query.expressions import F

    async with wall_clock_context():
        await WallClockEntry.objects.create(id=1, lasts=datetime.timedelta(hours=2))
        refused = (
            F("lasts") + 3,
            3 - F("lasts"),
            Decimal("2.5") + F("lasts"),
            3 / F("lasts"),
            F("lasts") * F("lasts"),
        )
        for expression in refused:
            with pytest.raises(FieldError, match="duration"):
                await WallClockEntry.objects.annotate(value=expression).values_list("value", flat=True)
        computed = await WallClockEntry.objects.annotate(
            doubled=F("lasts") * 2, halved=F("lasts") / 2, summed=F("lasts") + F("lasts")
        ).values_list("doubled", "halved", "summed")
        assert computed == [(datetime.timedelta(hours=4), datetime.timedelta(hours=1), datetime.timedelta(hours=4))]


@pytest.mark.asyncio
async def test_an_interval_takes_a_timedelta_and_a_numeric_column():
    """An interval plus a timedelta, and an interval scaled by a numeric column, were refused with a
    FieldError - PostgreSQL computes them, as hare does a TimeDeltaField's."""
    from hare.exceptions import FieldError
    from hare.query.expressions import F

    async with wall_clock_context():
        await WallClockEntry.objects.create(id=3, lasts=datetime.timedelta(hours=2))
        minute = datetime.timedelta(minutes=1)
        lasts = datetime.timedelta(hours=2)
        # A second shift runs the cached plan with the other value.
        for shift in (minute, minute * 2):
            computed = await WallClockEntry.objects.annotate(
                later=F("lasts") + shift, earlier=F("lasts") - shift, turned=shift - F("lasts")
            ).values_list("later", "earlier", "turned")
            assert computed == [(lasts + shift, lasts - shift, shift - lasts)]
        scaled = await WallClockEntry.objects.annotate(
            tripled=F("lasts") * F("id"), turned=F("id") * F("lasts"), third=F("lasts") / F("id")
        ).values_list("tripled", "turned", "third")
        assert scaled == [(datetime.timedelta(hours=6), datetime.timedelta(hours=6), datetime.timedelta(minutes=40))]
        await WallClockEntry.objects.filter(id=3).update(lasts=F("lasts") + minute)
        assert (await WallClockEntry.objects.get(id=3)).lasts == datetime.timedelta(hours=2, minutes=1)
        assert await WallClockEntry.objects.filter(lasts__gt=F("lasts") - minute).count() == 1
        with pytest.raises(FieldError, match="duration"):
            await WallClockEntry.objects.annotate(value=F("lasts") * minute).values_list("value", flat=True)


@pytest.mark.asyncio
async def test_a_timedelta_beside_an_interval_in_a_function_is_an_interval():
    """A timedelta beside an interval column in Coalesce/Greatest/Least/NullIf/Case was bound as its
    microseconds - a raw error of PostgreSQL or of the driver."""
    from hare.query.expressions import Case, F, Q, When
    from hare.query.functions import Coalesce, Greatest, Least, NullIf

    async with wall_clock_context():
        await WallClockEntry.objects.create(id=4, lasts=datetime.timedelta(hours=1))
        await WallClockEntry.objects.create(id=5)
        hour = datetime.timedelta(hours=1)
        # A second value runs the cached plan with the other literal.
        for other in (datetime.timedelta(hours=4), datetime.timedelta(minutes=30)):
            computed = (
                await WallClockEntry.objects.order_by("id")
                .annotate(
                    first=Coalesce(F("lasts"), other),
                    most=Greatest(F("lasts"), other),
                    least=Least(F("lasts"), other),
                    unless=NullIf(F("lasts"), hour),
                    chosen=Case(When(Q(id=4), then=other), default=F("lasts")),
                )
                .values_list("first", "most", "least", "unless", "chosen")
            )
            assert computed == [
                (hour, max(hour, other), min(hour, other), None, other),
                (other, other, other, None, None),
            ]


@pytest.mark.asyncio
async def test_a_moment_is_shifted_by_an_interval_column():
    """A datetime or a date plus an interval column was refused - PostgreSQL adds them."""
    from hare.query.expressions import F

    async with wall_clock_context():
        at = datetime.datetime(2024, 1, 31, 10, 0)
        await WallClockEntry.objects.create(id=1, at=at, lasts=datetime.timedelta(days=1, hours=2))
        shifted = await WallClockEntry.objects.annotate(
            later=F("at") + F("lasts"), earlier=F("at") - F("lasts"), turned=F("lasts") + F("at")
        ).values_list("later", "earlier", "turned")
        assert shifted == [
            (
                datetime.datetime(2024, 2, 1, 12, 0),
                datetime.datetime(2024, 1, 30, 8, 0),
                datetime.datetime(2024, 2, 1, 12, 0),
            )
        ]
        await WallClockEntry.objects.filter(id=1).update(at=F("at") + F("lasts"))
        assert (await WallClockEntry.objects.get(id=1)).at == datetime.datetime(2024, 2, 1, 12, 0)
        assert await WallClockEntry.objects.filter(at__lt=F("at") + F("lasts")).count() == 1


@pytest.mark.asyncio
async def test_an_interval_is_aggregated_by_array_agg_not_jsonb_agg():
    """JSONB_AGG wrote an interval as text of the server's IntervalStyle - the field read none of it."""
    from hare.dialects.postgresql.functions import JSONBAgg
    from hare.exceptions import FieldError
    from hare.query.functions import ArrayAgg

    async with wall_clock_context():
        await WallClockEntry.objects.create(id=1, lasts=datetime.timedelta(days=1, microseconds=5))
        with pytest.raises(FieldError, match=r"JSONBAgg\(\) can't aggregate IntervalField"):
            await WallClockEntry.objects.aggregate(lasts=JSONBAgg("lasts"))
        assert await WallClockEntry.objects.aggregate(lasts=ArrayAgg("lasts")) == {
            "lasts": [datetime.timedelta(days=1, microseconds=5)]
        }
