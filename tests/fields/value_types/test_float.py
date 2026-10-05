import math
from decimal import Decimal

import pytest

from hare.exceptions import ValidationError
from tests.shared_models import testmodels


@pytest.mark.asyncio
async def test_nan_is_refused_where_it_would_become_null(db):
    """SQLite stores NaN as NULL - refused there; another database stores it."""
    if testmodels.FloatFields._meta.connection.dialect.name == "sqlite":
        with pytest.raises(ValidationError, match="NaN"):
            await testmodels.FloatFields.objects.create(floatnum=1.0, floatnum_null=math.nan)
        return
    created = await testmodels.FloatFields.objects.create(floatnum=1.0, floatnum_null=math.nan)
    assert math.isnan((await testmodels.FloatFields.objects.get(id=created.id)).floatnum_null)


@pytest.mark.asyncio
async def test_cast_int(db):
    obj0 = await testmodels.FloatFields.objects.create(floatnum=123)
    obj = await testmodels.FloatFields.objects.get(id=obj0.id)
    assert obj.floatnum == 123


@pytest.mark.asyncio
async def test_cast_decimal(db):
    obj0 = await testmodels.FloatFields.objects.create(floatnum=Decimal("1.23"))
    obj = await testmodels.FloatFields.objects.get(id=obj0.id)
    assert obj.floatnum == 1.23


@pytest.mark.asyncio
async def test_a_decimal_past_the_float_range_is_refused(db):
    """float() made it an infinity, stored and compared silently - an int past the range raised."""
    huge = Decimal("1e500")
    obj = await testmodels.FloatFields.objects.create(floatnum=1.0)
    for refused in (
        lambda: testmodels.FloatFields.objects.create(floatnum=huge),
        lambda: testmodels.FloatFields.objects.bulk_create([testmodels.FloatFields(id=2, floatnum=-huge)]),
        lambda: testmodels.FloatFields.objects.filter(id=obj.id).update(floatnum=huge),
        lambda: testmodels.FloatFields.objects.filter(floatnum=huge).count(),
        lambda: testmodels.FloatFields.objects.filter(floatnum__lt=huge).count(),
        lambda: testmodels.FloatFields.objects.filter(floatnum__in=[huge]).count(),
    ):
        with pytest.raises(ValidationError, match="Decimal too large to convert to float"):
            await refused()
    assert await testmodels.FloatFields.objects.filter(floatnum__lt=Decimal("Infinity")).count() == 1
