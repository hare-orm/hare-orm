from __future__ import annotations

from typing import Any, ClassVar

from hare.exceptions import ValidationError
from hare.gis.constants import MAX_SRID
from hare.gis.enums import SpatialFunctionType
from hare.gis.fields.geometry_field import GeometryField
from hare.gis.functions.spatial_function import SpatialFunction
from hare.query.expressions import ExpressionResult


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

    #: The field the result is read through - a planar geometry carrying its own SRID. Shared: the
    #: statement plans hold output fields weakly.
    OUTPUT_FIELD: ClassVar[GeometryField] = GeometryField()

    def __init__(self, expression: Any, srid: int) -> None:
        if type(srid) is not int or not 1 <= srid <= MAX_SRID:
            raise ValidationError(f"Transform srid must be an int from 1 to {MAX_SRID}, got {srid!r}")
        super().__init__(expression, srid)

    def _get_output_field(self, function_arg: ExpressionResult, default_results: list[ExpressionResult]) -> Any:
        return self.OUTPUT_FIELD  # type: ignore[call-overload]
