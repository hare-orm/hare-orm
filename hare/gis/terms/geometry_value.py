from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.sql.terms.functions.function import Function
from hare.sql.terms.values.value_wrapper import ValueWrapper

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.gis.geometries.geometry import Geometry


class GeometryValue(Function):
    """A geometry given from Python - each dialect binds it in the form its column type reads (EWKT on
    PostGIS, SpatiaLite's own BLOB on SQLite), transformed to the column's SRID when it has another.

    Args:
        geometry: The geometry, with its SRID.
        target_srid: The SRID of the column it meets.
        geography: Whether the column is a geography - a geometry otherwise.
    """

    requires_dialect_renderer = True

    def __init__(self, geometry: Geometry, target_srid: int, geography: bool, alias: str | None = None) -> None:
        super().__init__("GEOMETRY_VALUE", ValueWrapper(geometry.ewkt), alias=alias)
        self.geometry = geometry
        self.srid: int = geometry.srid  # type: ignore[assignment]
        self.target_srid = target_srid
        self.geography = geography

    def get_value(self) -> Any:
        """The bound EWKT."""
        return self.args[0].value  # type: ignore[attr-defined]
