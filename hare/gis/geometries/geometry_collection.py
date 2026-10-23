from __future__ import annotations

from typing import Any, ClassVar

from hare.exceptions import ValidationError
from hare.gis.constants import GEOMETRY_MAX_NESTING_DEPTH, GEOMETRY_NESTING_DEPTH_MESSAGE
from hare.gis.enums import GeometryType
from hare.gis.geometries.geometry import Geometry
from hare.gis.geometries.multi_geometry import MultiGeometry


class GeometryCollection(MultiGeometry):
    """Geometries of any types - ``GeometryCollection([Point(0, 0), LineString([(0, 0), (1, 1)])])``.

    Args:
        members: The geometries.
        srid: The spatial reference system.
    """

    __slots__ = ()

    GEOMETRY_TYPE: ClassVar[GeometryType] = GeometryType.GEOMETRYCOLLECTION
    MEMBER_CLASS: ClassVar[type[Geometry]] = Geometry

    def get_member(self, member: Any) -> Geometry:
        if not isinstance(member, Geometry):
            raise ValidationError(f"A member of a GEOMETRYCOLLECTION is a geometry, got {member!r}")
        if isinstance(member, GeometryCollection) and member.get_nesting_depth() >= GEOMETRY_MAX_NESTING_DEPTH:
            raise ValidationError(GEOMETRY_NESTING_DEPTH_MESSAGE.format(depth=GEOMETRY_MAX_NESTING_DEPTH))
        return member.with_srid(None)

    def get_nesting_depth(self) -> int:
        """The collections nested in one another from this one down - 1 for one holding none.

        Returns:
            The depth.
        """
        member_depths = (
            member.get_nesting_depth() for member in self.members if isinstance(member, GeometryCollection)
        )
        return 1 + max(member_depths, default=0)

    @classmethod
    def from_coordinates(cls, coordinates: Any, srid: int | None = None) -> GeometryCollection:
        # Only the empty collection has coordinates - none.
        if coordinates != ():
            raise ValidationError("A GEOMETRYCOLLECTION has geometries, not coordinates")
        return cls(srid=srid)

    def get_coordinates(self) -> tuple[Any, ...]:
        # The members themselves - their types are part of the collection, unlike a multi
        # geometry's, whose members all share one type.
        return self.members

    def get_wkt_body(self) -> str:
        if not self.members:
            return "EMPTY"
        return f"({','.join(member.wkt for member in self.members)})"

    @property
    def __geo_interface__(self) -> dict[str, Any]:
        return {"type": "GeometryCollection", "geometries": [member.__geo_interface__ for member in self.members]}
