import datetime

import pytest

from hare.exceptions import ValidationError
from hare.query.expressions import F
from hare.query.functions import Avg, Max, Min, Sum
from tests import testmodels


@pytest.mark.asyncio
async def test_create_and_read_back(db):
    value = datetime.timedelta(days=5, hours=3, minutes=10, seconds=1, microseconds=500)
    obj = await testmodels.TimeDeltaFields.objects.create(timedelta=value)
    reread = await testmodels.TimeDeltaFields.objects.get(id=obj.id)
    assert reread.timedelta == value
    assert reread.timedelta_null is None


@pytest.mark.asyncio
async def test_overflowing_the_int64_microsecond_storage_raises_validation_error(db):
    # datetime.timedelta's own range (up to +-999999999 days) vastly exceeds what fits in this
    # field's BIGINT-microseconds storage - each backend used to raise a different raw, unwrapped
    # exception for the identical overflow instead of one catchable ValidationError.
    with pytest.raises(ValidationError, match="out of range"):
        await testmodels.TimeDeltaFields.objects.create(timedelta=datetime.timedelta(days=999999999))


@pytest.mark.asyncio
async def test_boundary_values_round_trip(db):
    near_max = datetime.timedelta(microseconds=2**63 - 1)
    near_min = datetime.timedelta(microseconds=-(2**63))

    obj_max = await testmodels.TimeDeltaFields.objects.create(timedelta=near_max)
    obj_min = await testmodels.TimeDeltaFields.objects.create(timedelta=near_min)

    assert (await testmodels.TimeDeltaFields.objects.get(id=obj_max.id)).timedelta == near_max
    assert (await testmodels.TimeDeltaFields.objects.get(id=obj_min.id)).timedelta == near_min


@pytest.mark.asyncio
async def test_one_microsecond_past_the_boundary_raises_validation_error(db):
    with pytest.raises(ValidationError, match="out of range"):
        await testmodels.TimeDeltaFields.objects.create(timedelta=datetime.timedelta(microseconds=2**63))


@pytest.mark.asyncio
async def test_sum_avg_min_max_aggregate(db):
    """Sum()/Avg() over a TimeDeltaField used to crash with `TypeError: unsupported type for
    timedelta microseconds component: decimal.Decimal` on Postgres, both drivers - SUM(bigint)/
    AVG(bigint) return NUMERIC there, decoded as a Decimal by both drivers, and `datetime.
    timedelta(microseconds=...)` only accepts an int/float. SQLite (INTEGER/REAL) was never
    affected."""
    await testmodels.TimeDeltaFields.objects.create(timedelta=datetime.timedelta(seconds=10))
    await testmodels.TimeDeltaFields.objects.create(timedelta=datetime.timedelta(seconds=20))
    await testmodels.TimeDeltaFields.objects.create(timedelta=datetime.timedelta(seconds=30))

    result = await testmodels.TimeDeltaFields.objects.all().aggregate(
        total=Sum("timedelta"), avg=Avg("timedelta"), lo=Min("timedelta"), hi=Max("timedelta")
    )
    assert result["total"] == datetime.timedelta(seconds=60)
    assert result["avg"] == datetime.timedelta(seconds=20)
    assert result["lo"] == datetime.timedelta(seconds=10)
    assert result["hi"] == datetime.timedelta(seconds=30)

    annotated = (
        await testmodels.TimeDeltaFields.objects.all()
        .values("timedelta_null")
        .annotate(total=Sum("timedelta"))
        .values("total")
    )
    assert annotated[0]["total"] == datetime.timedelta(seconds=60)


@pytest.mark.asyncio
async def test_avg_rounds_a_fractional_number_of_microseconds(db):
    await testmodels.TimeDeltaFields.objects.create(timedelta=datetime.timedelta(microseconds=1))
    await testmodels.TimeDeltaFields.objects.create(timedelta=datetime.timedelta(microseconds=2))
    result = await testmodels.TimeDeltaFields.objects.all().aggregate(avg=Avg("timedelta"))
    assert result["avg"] == datetime.timedelta(microseconds=round((1 + 2) / 2))


@pytest.mark.asyncio
async def test_f_expression_addition_of_two_timedelta_fields(db):
    obj = await testmodels.TimeDeltaFields.objects.create(
        timedelta=datetime.timedelta(seconds=5), timedelta_null=datetime.timedelta(seconds=7)
    )
    result = (
        await testmodels.TimeDeltaFields.objects.filter(id=obj.id)
        .annotate(combined=F("timedelta") + F("timedelta_null"))
        .values("combined")
    )
    assert result[0]["combined"] == datetime.timedelta(seconds=12)
