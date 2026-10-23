import datetime

import pytest

from hare.query.functions import ExtractYear, TruncYear
from tests.shared_models import testmodels


@pytest.mark.asyncio
async def test_create(db):
    obj0 = await testmodels.DateFields.objects.create(date=datetime.date(2020, 5, 21))
    obj = await testmodels.DateFields.objects.get(id=obj0.id)
    assert obj.date == datetime.date(2020, 5, 21)
    assert obj.date_null is None


@pytest.mark.asyncio
async def test_datetime_value_truncated_to_date(db):
    """DateField.to_db_value/from_db_value only special-cased str input - a raw
    datetime.datetime (itself a datetime.date subclass, so it passed the old isinstance(value,
    datetime.date) check unchanged) went through untouched instead of being narrowed to a plain
    date, silently carrying a time component into a DATE column."""
    obj0 = await testmodels.DateFields.objects.create(date=datetime.datetime(2020, 5, 21, 23, 59, 1))
    assert type(obj0.date) is datetime.date
    assert obj0.date == datetime.date(2020, 5, 21)

    obj = await testmodels.DateFields.objects.get(id=obj0.id)
    assert type(obj.date) is datetime.date
    assert obj.date == datetime.date(2020, 5, 21)

    obj.date = datetime.datetime(2021, 1, 2, 3, 4, 5)
    await obj.save()
    reloaded = await testmodels.DateFields.objects.get(id=obj.id)
    assert type(reloaded.date) is datetime.date
    assert reloaded.date == datetime.date(2021, 1, 2)


@pytest.mark.asyncio
async def test_constructor_kwargs_datetime_truncated(db):
    obj = testmodels.DateFields(date=datetime.datetime(2020, 5, 21, 12, 0, 0))
    assert type(obj.date) is datetime.date
    assert obj.date == datetime.date(2020, 5, 21)


@pytest.mark.asyncio
async def test_the_extreme_dates_are_dates_to_the_date_functions(db):
    """PostgreSQL got date.max/date.min as infinity: EXTRACT of it failed the whole query, a year
    9999 filter missed the row and TRUNC kept 9999-12-31. They are written as the days they are."""
    await testmodels.DateFields.objects.create(id=1, date=datetime.date.max)
    await testmodels.DateFields.objects.create(id=2, date=datetime.date.min)
    rows = (
        testmodels.DateFields.objects.order_by("id")
        .annotate(year=ExtractYear("date"), year_start=TruncYear("date"))
        .values_list("id", "date", "year", "year_start")
    )
    assert await rows == [
        (1, datetime.date.max, 9999, datetime.date(9999, 1, 1)),
        (2, datetime.date.min, 1, datetime.date.min),
    ]
    assert await testmodels.DateFields.objects.filter(date__year=9999).values_list("id", flat=True) == [1]
    assert await testmodels.DateFields.objects.filter(date__year=1).values_list("id", flat=True) == [2]
