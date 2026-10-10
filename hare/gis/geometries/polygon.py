from __future__ import annotations

from collections.abc import Sequence
from typing import Any, ClassVar

from hare.exceptions import ValidationError
from hare.gis.enums import GeometryType
from hare.gis.geometries.geometry import Geometry


class Polygon(Geometry):
    """An area bounded by an exterior ring, with optional holes - each ring a closed sequence of four
    or more positions (the first repeated last). ``Polygon()`` is the empty polygon.

    Example: ``Polygon([(0, 0), (4, 0), (4, 4), (0, 4), (0, 0)], holes=[[(1, 1), (2, 1), (2, 2), (1, 1)]])``

    Args:
        exterior: The exterior ring.
        holes: The interior rings.
        srid: The spatial reference system.
    """

    __slots__ = ("rings",)

    GEOMETRY_TYPE: ClassVar[GeometryType] = GeometryType.POLYGON

    def __init__(self, exterior: Sequence[Any] = (), holes: Sequence[Sequence[Any]] = (), *, srid: int | None = None):
        super().__init__(srid)
        exterior_ring = Polygon.get_ring(exterior)
        if not exterior_ring and holes:
            raise ValidationError("An empty polygon has no holes")
        rings = (exterior_ring, *(Polygon.get_ring(hole) for hole in holes)) if exterior_ring else ()
        if len({len(ring[0]) for ring in rings}) > 1:
            raise ValidationError("The rings of a polygon mix 2 and 3 coordinates")
        self.rings = rings

    @staticmethod
    def get_ring(positions: Sequence[Any]) -> tuple[tuple[float, ...], ...]:
        """One ring - closed, four or more positions, or empty.

        Args:
            positions: The ring's positions.

        Returns:
            The positions.

        Raises:
            ValidationError: The ring has one to three positions or isn't closed.
        """
        ring = Geometry.get_same_dimension_coordinates(positions, "ring")
        if ring and (len(ring) < 4 or ring[0] != ring[-1]):
            raise ValidationError(f"A polygon ring is closed and has four or more positions, got {list(ring)!r}")
        return ring

    @classmethod
    def from_coordinates(cls, coordinates: Any, srid: int | None = None) -> Polygon:
        if isinstance(coordinates, (str, bytes)) or not isinstance(coordinates, Sequence):
            raise ValidationError(f"A polygon is a sequence of rings, got {coordinates!r}")
        if not coordinates:
            return cls(srid=srid)
        return cls(coordinates[0], coordinates[1:], srid=srid)

    @property
    def exterior(self) -> tuple[tuple[float, ...], ...]:
        """The exterior ring, empty for the empty polygon."""
        return self.rings[0] if self.rings else ()

    @property
    def holes(self) -> tuple[tuple[tuple[float, ...], ...], ...]:
        """The interior rings."""
        return self.rings[1:]

    def get_coordinates(self) -> tuple[tuple[tuple[float, ...], ...], ...]:
        return self.rings

    @property
    def has_z(self) -> bool:
        return bool(self.rings) and len(self.rings[0][0]) == 3

    def get_wkt_body(self) -> str:
        if not self.rings:
            return "EMPTY"
        return f"({','.join(Geometry.format_positions(ring) for ring in self.rings)})"
