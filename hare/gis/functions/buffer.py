from __future__ import annotations

from typing import Any, ClassVar

from hare.exceptions import ValidationError
from hare.gis.enums import SpatialFunctionType
from hare.gis.functions.spatial_function import SpatialFunction
from hare.numbers.finite_numbers import FiniteNumbers


class Buffer(SpatialFunction):
    """``Buffer(geometry, distance)`` - the area within ``distance`` of the geometry, in meters on a
    geography and in the SRID's units on a geometry.

    Args:
        expression: The geometry - a field name, ``F()`` or an expression.
        distance: The distance, a finite number; a negative one shrinks a polygon.

    Raises:
        ValidationError: ``distance`` isn't a finite number.
    """

    function_type: ClassVar[SpatialFunctionType] = SpatialFunctionType.BUFFER

    def __init__(self, expression: Any, distance: float) -> None:
        if not FiniteNumbers.is_finite_number(distance):
            raise ValidationError(f"Buffer distance must be a finite number, got {distance!r}")
        super().__init__(expression, float(distance))
