import datetime

import pytest

from tests import testmodels


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
