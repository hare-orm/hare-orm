"""datetime.min and datetime.max without use_timezone: written, read back and found as the naive extreme -
not as a local wall clock, which east or west of UTC falls past the datetime range."""

import datetime

import pytest

from hare.core.hare_context import HareContext
from tests.shared_models.testmodels import DatetimeFields
from tests.utils.timezone_context import override_timezone


@pytest.mark.parametrize(
    "value",
    [
        datetime.datetime.min,
        datetime.datetime.max,
        datetime.datetime.min.replace(tzinfo=datetime.UTC),
        datetime.datetime.max.replace(tzinfo=datetime.UTC),
    ],
)
@pytest.mark.asyncio
async def test_the_datetime_extremes_read_back_without_use_timezone(db, value):
    """On PostgreSQL a naive datetime.min reached the driver, which bound it as a local wall clock - a moment
    before year 1 east of UTC, a row every read of the table failed on; on SQLite a filter by the aware
    extreme compared a local time with the stored naive extreme."""
    if HareContext.get_current().connections.get("models").dialect.name == "clickhouse":
        pytest.skip("ClickHouse's DateTime64 holds 1900 to 2299")
    with override_timezone(use_timezone=False):
        obj = await DatetimeFields.objects.create(id=1, datetime=value)
        expected = value.replace(tzinfo=None)
        assert obj.datetime == expected
        assert (await DatetimeFields.objects.get(id=1)).datetime == expected
        assert await DatetimeFields.objects.values_list("datetime", flat=True) == [expected]
        assert await DatetimeFields.objects.filter(datetime=value).count() == 1
