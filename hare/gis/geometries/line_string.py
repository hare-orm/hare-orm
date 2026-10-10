from __future__ import annotations

from collections.abc import Sequence
from typing import Any, ClassVar

from hare.exceptions import ValidationError
from hare.gis.enums import GeometryType
from hare.gis.geometries.geometry import Geometry


class LineString(Geometry):
    """A line through two or more positions - ``LineString([(0, 0), (1, 1)])``; each position a
    ``Point`` or a sequence of numbers. ``LineString([])`` is the empty line.

    Args:
        positions: The positions.
        srid: The spatial reference system.
    """

    __slots__ = ("positions",)

    GEOMETRY_TYPE: ClassVar[GeometryType] = GeometryType.LINESTRING

    def __init__(self, positions: Sequence[Any] = (), *, srid: int | None = None) -> None:
        super().__init__(srid)
        line_positions = Geometry.get_same_dimension_coordinates(positions, "line")
        if len(line_positions) == 1:
            raise ValidationError("A line has two or more positions, got one")
        self.positions = line_positions

    @classmethod
    def from_coordinates(cls, coordinates: Any, srid: int | None = None) -> LineString:
        return cls(coordinates, srid=srid)

    def get_coordinates(self) -> tuple[tuple[float, ...], ...]:
        return self.positions

    @property
    def has_z(self) -> bool:
        return bool(self.positions) and len(self.positions[0]) == 3

    def get_wkt_body(self) -> str:
        return Geometry.format_positions(self.positions) if self.positions else "EMPTY"
