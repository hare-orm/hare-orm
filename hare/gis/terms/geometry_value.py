from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.sql.terms.functions.function import Function
from hare.sql.terms.values.value_wrapper import ValueWrapper

if TYPE_CHECKING:
    from hare.dialects.base.dialect import Dialect
    from hare.gis.geometries.geometry import Geometry


class GeometryValue(Function):
    """A geometry given from Python - each dialect binds it in the form its column type reads,
    transformed to the column's SRID when it has another.

    Args:
        geometry: The geometry, with its SRID.
        target_srid: The SRID of the column it meets.
        geography: Whether the column is a geography - a geometry otherwise.
        dialect: The dialect preparing this bound input.
    """

    requires_dialect_renderer = True

    def __init__(
        self,
        geometry: Geometry,
        target_srid: int | None,
        geography: bool,
        alias: str | None = None,
        *,
        dialect: Dialect,
    ) -> None:
        from hare.gis.geometry_arguments import GeometryArguments

        super().__init__(
            "GEOMETRY_VALUE", ValueWrapper(GeometryArguments.encode_parameter(geometry, dialect)), alias=alias
        )
        self.geometry = geometry
        self.srid = geometry.srid
        self.target_srid = target_srid
        self.geography = geography

    def get_value(self) -> Any:
        """The final native input parameter, shared with plan recording."""
        return self.args[0].value  # type: ignore[attr-defined]
