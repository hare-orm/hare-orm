from __future__ import annotations

import math
from typing import Any, ClassVar

from hare.exceptions import ValidationError
from hare.gis.enums import SpatialFunctionType
from hare.gis.functions.spatial_function import SpatialFunction


class Simplify(SpatialFunction):
    """``Simplify(geometry, tolerance)`` - the geometry with fewer points (Douglas-Peucker), no point
    moved further than ``tolerance`` in the SRID's units.

    Args:
        expression: The geometry - a field name, ``F()`` or an expression.
        tolerance: The tolerance, a finite number of zero or more.

    Raises:
        ValidationError: ``tolerance`` isn't such a number.
    """

    function_type: ClassVar[SpatialFunctionType] = SpatialFunctionType.SIMPLIFY

    def __init__(self, expression: Any, tolerance: float) -> None:
        if isinstance(tolerance, bool) or not isinstance(tolerance, (int, float)) or not 0 <= tolerance < math.inf:
            raise ValidationError(f"Simplify tolerance must be a finite number of zero or more, got {tolerance!r}")
        super().__init__(expression, float(tolerance))
