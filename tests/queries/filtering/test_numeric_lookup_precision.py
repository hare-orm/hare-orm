"""Regression tests for IntField/DecimalField lookups silently truncating or rounding a
non-integer/higher-precision filter value to the field's own storage precision before comparing,
instead of comparing against the value actually passed in."""

from decimal import Decimal

import pytest

from hare.exceptions import ValidationError
from tests.shared_models.testmodels import DecimalFields, IntFields


async def _create_int_fields() -> None:
    await IntFields.objects.create(intnum=1)
    await IntFields.objects.create(intnum=2)
    await IntFields.objects.create(intnum=3)


@pytest.mark.asyncio
async def test_int_field_equality_and_membership_take_an_int_alone(db):
    """A float or a Decimal - whole or not - is no value an int column holds: equality and
    membership refuse it, as a write does; ClickHouse failed on a Decimal in IN."""
    await _create_int_fields()

    for value in (2.5, 2.0, Decimal("2"), Decimal("2.5"), True):
        for lookup in ("intnum", "intnum__not"):
            with pytest.raises(ValidationError, match="intnum: expected an int"):
                await IntFields.objects.filter(**{lookup: value}).count()
        for lookup in ("intnum__in", "intnum__not_in", "id__in"):
            with pytest.raises(ValidationError, match="expected an int"):
                await IntFields.objects.filter(**{lookup: [1, value]}).count()
    # A plan of an int value refuses a float too.
    assert await IntFields.objects.filter(intnum=2).count() == 1
    with pytest.raises(ValidationError, match="intnum: expected an int"):
        await IntFields.objects.filter(intnum=2.0).count()
    assert await IntFields.objects.filter(intnum__in=[2]).count() == 1
    with pytest.raises(ValidationError, match="intnum: expected an int"):
        await IntFields.objects.filter(intnum__in=[2.0]).count()


@pytest.mark.asyncio
async def test_a_comparison_of_an_int_field_takes_a_fraction_after_an_int(db):
    """A plan built for an int bound binds a whole float as the int, rebuilds for a fraction."""
    await _create_int_fields()

    assert await IntFields.objects.filter(intnum__gt=2).count() == 1
    assert await IntFields.objects.filter(intnum__gt=1.5).count() == 2
    assert await IntFields.objects.filter(intnum__gt=Decimal("1.0")).count() == 2
    assert await IntFields.objects.filter(intnum__range=(1, 2)).count() == 2
    assert await IntFields.objects.filter(intnum__range=(1.5, 3)).count() == 2
    with pytest.raises(ValidationError, match="NaN"):
        await IntFields.objects.filter(intnum__lte=float("nan")).count()


@pytest.mark.asyncio
async def test_int_field_gte_with_non_integer_value(db):
    await _create_int_fields()

    result = sorted([row.intnum async for row in IntFields.objects.filter(intnum__gte=2.5)])

    assert result == [3]


@pytest.mark.asyncio
async def test_int_field_lt_with_non_integer_value(db):
    await _create_int_fields()

    result = sorted([row.intnum async for row in IntFields.objects.filter(intnum__lt=2.5)])

    assert result == [1, 2]


@pytest.mark.asyncio
async def test_int_field_range_with_non_integer_bounds(db):
    await _create_int_fields()

    result = sorted([row.intnum async for row in IntFields.objects.filter(intnum__range=(1.5, 2.5))])

    assert result == [2]


async def _create_decimal_fields() -> None:
    await DecimalFields.objects.create(decimal=Decimal("1.2344"), decimal_nodec=1)
    await DecimalFields.objects.create(decimal=Decimal("1.2345"), decimal_nodec=1)
    await DecimalFields.objects.create(decimal=Decimal("1.2346"), decimal_nodec=1)


@pytest.mark.asyncio
async def test_decimal_field_gt_with_extra_precision_bound(db):
    """decimal__gt=Decimal("1.23456") used to quantize the bound to 4 decimal places
    (decimal_places=4) before comparing - 1.23456 rounds to 1.2346, so `decimal > 1.2346` wrongly
    excluded the row storing exactly 1.2346. The real, unrounded bound (1.23456) is below it."""
    await _create_decimal_fields()

    result = [str(row.decimal) async for row in DecimalFields.objects.filter(decimal__gt=Decimal("1.23456"))]

    assert result == ["1.2346"]


@pytest.mark.asyncio
async def test_decimal_field_equality_with_extra_precision_value_matches_nothing(db):
    """decimal=Decimal("1.23454") used to quantize to 1.2345 before comparing, wrongly matching
    the row storing 1.2345 - no stored value can ever equal 1.23454 exactly."""
    await _create_decimal_fields()

    assert await DecimalFields.objects.filter(decimal=Decimal("1.23454")).count() == 0


@pytest.mark.asyncio
async def test_decimal_field_lte_with_extra_precision_bound_excludes_rounded_up_row(db):
    """decimal__lte=Decimal("1.234599") used to quantize the bound to 1.2346 before comparing,
    wrongly including the row storing exactly 1.2346 - the real, unrounded bound (1.234599) is
    below it."""
    await _create_decimal_fields()

    result = sorted([str(row.decimal) async for row in DecimalFields.objects.filter(decimal__lte=Decimal("1.234599"))])

    assert result == ["1.2344", "1.2345"]


@pytest.mark.asyncio
async def test_decimal_field_with_decimal_places_zero_gt_with_fractional_bound(db):
    """decimal_nodec has decimal_places=0 - a fractional lookup bound (1.5) used to round to the
    nearest whole number (2) before comparing, changing which rows a `>`/`<` lookup matched."""
    await DecimalFields.objects.create(decimal=Decimal("1.0000"), decimal_nodec=Decimal("1"))
    await DecimalFields.objects.create(decimal=Decimal("2.0000"), decimal_nodec=Decimal("2"))

    result = sorted([int(row.decimal_nodec) async for row in DecimalFields.objects.filter(decimal_nodec__gt=1.5)])

    assert result == [2]
