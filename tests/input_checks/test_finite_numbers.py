"""A number an argument takes is finite only when a float can hold it - an int beyond the float range
is refused like NaN and the infinities, with the argument's own error, not OverflowError."""

import pytest

from hare.exceptions import QueryError, ValidationError
from hare.gis.geometries.point import Point
from hare.numbers.finite_numbers import FiniteNumbers
from tests.shared_models.testmodels import IntFields

HUGE_INT = 10**400


@pytest.mark.database_independent
@pytest.mark.parametrize("value", [0, -3, 2.5, 10**300])
def test_a_finite_number(value):
    assert FiniteNumbers.is_finite_number(value) is True


@pytest.mark.database_independent
@pytest.mark.parametrize(
    "value", [True, False, float("nan"), float("inf"), -float("inf"), HUGE_INT, -HUGE_INT, "1", None]
)
def test_not_a_finite_number(value):
    assert FiniteNumbers.is_finite_number(value) is False


@pytest.mark.asyncio
async def test_arguments_refuse_an_int_beyond_the_float_range(db):
    with pytest.raises(QueryError, match="takes a percent"):
        IntFields.objects.sample(HUGE_INT)
    with pytest.raises(ValidationError, match="A coordinate is a finite number"):
        Point(HUGE_INT, 1, srid=4326)
