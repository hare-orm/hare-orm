from __future__ import annotations

from collections.abc import Sequence
from typing import Any, ClassVar

from hare.exceptions import ValidationError
from hare.gis.enums import GeometryType
from hare.gis.geometries.geometry import Geometry


class Point(Geometry):
    """A position - ``Point(x, y)``, ``Point(x, y, z)``; for longitude/latitude x is the longitude.
    ``Point()`` is the empty point.

    Args:
        x: The first coordinate.
        y: The second coordinate.
        z: The third coordinate, None for a 2D point.
        srid: The spatial reference system.
    """

    __slots__ = ("x", "y", "z")

    GEOMETRY_TYPE: ClassVar[GeometryType] = GeometryType.POINT

    def __init__(
        self, x: float | None = None, y: float | None = None, z: float | None = None, *, srid: int | None = None
    ):
        super().__init__(srid)
        self.x: float | None
        self.y: float | None
        self.z: float | None
        if x is None and y is None and z is None:
            self.x = self.y = self.z = None
            return
        if x is None or y is None:
            raise ValidationError(f"A point has both x and y, got x={x!r}, y={y!r}")
        position = Geometry.get_coordinate((x, y) if z is None else (x, y, z))
        self.x, self.y = position[0], position[1]
        self.z = position[2] if len(position) == 3 else None

    @classmethod
    def from_coordinates(cls, coordinates: Any, srid: int | None = None) -> Point:
        if isinstance(coordinates, Sequence) and not isinstance(coordinates, (str, bytes)) and not coordinates:
            return cls(srid=srid)
        return cls(*Geometry.get_coordinate(coordinates), srid=srid)

    def get_coordinates(self) -> tuple[float, ...]:
        if self.x is None or self.y is None:
            return ()
        return (self.x, self.y) if self.z is None else (self.x, self.y, self.z)

    @property
    def has_z(self) -> bool:
        return self.z is not None

    def get_wkt_body(self) -> str:
        coordinates = self.get_coordinates()
        return f"({Geometry.format_position(coordinates)})" if coordinates else "EMPTY"

    def get_geo_json_coordinates(self) -> Any:
        return list(self.get_coordinates())
