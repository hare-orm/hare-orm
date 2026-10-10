from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.gis.enums import SpatialFunctionType
from hare.sql.terms.functions.function import Function

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.gis.fields.geometry_field import GeometryField


class SpatialFunctionTerm(Function):
    """A spatial function - each dialect writes its own SQL.

    Args:
        function_type: What it computes.
        arguments: The geometries, then the function's own arguments.
        geography: Whether the first geometry is a geography.
        geometry_argument_count: How many of the arguments are geometries.
        geometry_fields: The field of each geometry argument, None where it has none - a dialect
            whose SQL depends on a geometry's type or SRID reads them from it.
    """

    requires_dialect_renderer = True

    def __init__(
        self,
        function_type: SpatialFunctionType,
        *arguments: Any,
        geography: bool = False,
        geometry_argument_count: int = 1,
        geometry_fields: tuple[GeometryField | None, ...] = (),
        alias: str | None = None,
    ) -> None:
        super().__init__("SPATIAL_FUNCTION", *arguments, alias=alias)
        self.function_type = function_type
        self.geography = geography
        self.geometry_argument_count = geometry_argument_count
        self.geometry_fields = geometry_fields
