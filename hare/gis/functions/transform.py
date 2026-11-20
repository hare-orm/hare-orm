from __future__ import annotations

from typing import Any, ClassVar

from hare.classes.value_descriptions import ValueDescriptions
from hare.exceptions import ValidationError
from hare.gis.constants import MAX_SRID
from hare.gis.enums import SpatialFunctionType
from hare.gis.functions.spatial_function import SpatialFunction


class Transform(SpatialFunction):
    """``Transform(geometry, srid)`` - the geometry's coordinates in another spatial reference system,
    read with that SRID.

    Args:
        expression: The geometry - a field name, ``F()`` or an expression.
        srid: The target SRID.

    Raises:
        ValidationError: ``srid`` isn't an int from 1 to the largest SRID.
    """

    function_type: ClassVar[SpatialFunctionType] = SpatialFunctionType.TRANSFORM
    default_value_counts = (1, 1)

    def __init__(self, expression: Any, srid: int) -> None:
        if type(srid) is not int or not 1 <= srid <= MAX_SRID:
            raise ValidationError(
                f"Transform srid must be an int from 1 to {MAX_SRID}, got {ValueDescriptions.get_shown_value(srid)}"
            )
        super().__init__(expression, srid)

    def get_geometry_plan_key(self) -> tuple[Any, ...]:
        """Keep the result SRID with the plan whose readers and enclosing SQL use it."""
        return (*super().get_geometry_plan_key(), self.default_values[0])
