"""dates() and datetimes() - the distinct truncated values of a date or datetime field, NULLs left out,
in either order - reverse() turning an ordering around exactly, and Meta.get_latest_by giving
latest()/earliest() their fields."""

from __future__ import annotations

import datetime

import pytest

from hare import Model, fields
from hare.exceptions import ConfigurationError, FieldError, QueryError
from hare.query.expressions import F
from hare.time import Timezone
from tests.testmodels import DateFields, DatetimeFields, IntFields, Team


async def create_dates() -> None:
    for row_id, (date, date_null) in enumerate(
        [
            (datetime.date(2024, 1, 15), None),
            (datetime.date(2024, 1, 20), datetime.date(2023, 5, 1)),
            (datetime.date(2024, 3, 2), None),
            (datetime.date(2025, 7, 9), datetime.date(2023, 5, 3)),
        ],
        start=1,
    ):
        await DateFields.objects.create(id=row_id, date=date, date_null=date_null)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("trunc_type", "expected"),
    [
        ("year", [datetime.date(2024, 1, 1), datetime.date(2025, 1, 1)]),
        ("quarter", [datetime.date(2024, 1, 1), datetime.date(2025, 7, 1)]),
        ("month", [datetime.date(2024, 1, 1), datetime.date(2024, 3, 1), datetime.date(2025, 7, 1)]),
        ("week", [datetime.date(2024, 1, 15), datetime.date(2024, 2, 26), datetime.date(2025, 7, 7)]),
        (
            "day",
            [
                datetime.date(2024, 1, 15),
                datetime.date(2024, 1, 20),
                datetime.date(2024, 3, 2),
                datetime.date(2025, 7, 9),
            ],
        ),
    ],
)
async def test_dates_of_a_date_field(db, trunc_type, expected):
    await create_dates()
    assert await DateFields.objects.dates("date", trunc_type) == expected
    assert await DateFields.objects.dates("date", trunc_type, order="DESC") == expected[::-1]


@pytest.mark.asyncio
async def test_dates_leave_nulls_out_and_follow_the_filter(db):
    await create_dates()
    assert await DateFields.objects.dates("date_null", "month") == [datetime.date(2023, 5, 1)]
    assert await DateFields.objects.filter(date__year=2024).dates("date", "year") == [datetime.date(2024, 1, 1)]


@pytest.mark.asyncio
async def test_dates_and_datetimes_of_a_datetime_field(db):
    moments = [
        datetime.datetime(2024, 1, 15, 10, 30, 5, tzinfo=datetime.UTC),
        datetime.datetime(2024, 1, 15, 22, 0, 0, tzinfo=datetime.UTC),
        datetime.datetime(2024, 2, 1, 8, 0, 0, tzinfo=datetime.UTC),
    ]
    for row_id, moment in enumerate(moments, start=1):
        await DatetimeFields.objects.create(id=row_id, datetime=moment)
    assert await DatetimeFields.objects.dates("datetime", "day") == [
        datetime.date(2024, 1, 15),
        datetime.date(2024, 2, 1),
    ]
    hours = await DatetimeFields.objects.datetimes("datetime", "hour")
    assert [Timezone.make_naive(hour, datetime.UTC) for hour in hours] == [
        datetime.datetime(2024, 1, 15, 10),
        datetime.datetime(2024, 1, 15, 22),
        datetime.datetime(2024, 2, 1, 8),
    ]
    months = await DatetimeFields.objects.datetimes("datetime", "month", order="DESC", tzinfo="UTC")
    assert [Timezone.make_naive(month, datetime.UTC) for month in months] == [
        datetime.datetime(2024, 2, 1),
        datetime.datetime(2024, 1, 1),
    ]
    assert await DatetimeFields.objects.datetimes("datetime_null", "day") == []


@pytest.mark.parametrize(
    ("call", "message"),
    [
        (lambda: DateFields.objects.dates("id", "day"), "dates\\(\\) takes a DateField or DatetimeField"),
        (lambda: DateFields.objects.dates("date", "hour"), "dates\\(\\) trunc_type must be one of"),
        (lambda: DateFields.objects.dates("date", "day", order="UP"), "order must be 'ASC' or 'DESC'"),
        (lambda: DateFields.objects.datetimes("date", "day"), "datetimes\\(\\) takes a DatetimeField"),
        (
            lambda: DatetimeFields.objects.datetimes("datetime", "fortnight"),
            "datetimes\\(\\) trunc_type must be one of",
        ),
    ],
)
def test_dates_refuse_a_wrong_field_type_or_order(call, message):
    with pytest.raises(FieldError, match=message):
        call()


@pytest.mark.asyncio
async def test_reverse_turns_the_ordering_around_exactly(db):
    for row_id, (value, nullable) in enumerate([(10, None), (20, 1), (30, None), (20, 2)], start=1):
        await IntFields.objects.create(id=row_id, intnum=value, intnum_null=nullable)
    ordered = IntFields.objects.order_by("intnum", "-id")
    forward = [row.id for row in await ordered]
    assert [row.id for row in await ordered.reverse()] == forward[::-1]
    assert [row.id for row in await ordered.reverse().reverse()] == forward
    with_nulls = IntFields.objects.order_by(F("intnum_null").asc(nulls_first=True), "id")
    assert [row.id for row in await with_nulls.reverse()] == [row.id for row in await with_nulls][::-1]
    # An unordered queryset stays unordered.
    assert IntFields.objects.all().reverse().orderings == ()


@pytest.mark.asyncio
async def test_reverse_turns_meta_ordering_around(db):
    for team_id in (2, 3, 1):
        await Team.objects.create(id=team_id, name=f"team {team_id}")
    # Team's Meta.ordering is ["id"].
    assert [team.id for team in await Team.objects.all()] == [1, 2, 3]
    assert [team.id for team in await Team.objects.all().reverse()] == [3, 2, 1]


@pytest.mark.asyncio
async def test_reverse_refuses_a_slice(db):
    with pytest.raises(QueryError, match="Cannot reverse a query once a slice has been taken"):
        IntFields.objects.order_by("id").limit(2).reverse()


@pytest.mark.asyncio
async def test_latest_and_earliest_take_meta_get_latest_by(db):
    await create_dates()
    assert (await DateFields.objects.latest()).id == 4
    assert (await DateFields.objects.earliest()).id == 1
    assert (await DateFields.objects.latest("date_null")).id == 4
    with pytest.raises(FieldError, match="latest\\(\\) needs the fields to order by"):
        await IntFields.objects.latest()


def test_meta_get_latest_by_takes_field_names_only():
    with pytest.raises(ConfigurationError, match="Meta.get_latest_by must be a field name"):

        class BadLatest(Model):
            id = fields.IntField(primary_key=True)

            class Meta:
                abstract = True
                get_latest_by = 5
