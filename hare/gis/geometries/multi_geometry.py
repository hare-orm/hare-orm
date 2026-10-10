from __future__ import annotations

from collections.abc import Sequence
from typing import Any, ClassVar

from hare.exceptions import ValidationError
from hare.gis.geometries.geometry import Geometry


class MultiGeometry(Geometry):
    """Base of the geometries made of members of one type - each member a geometry of
    ``MEMBER_CLASS`` or its coordinates. A member's own SRID is dropped: the collection's applies.

    Args:
        members: The members.
        srid: The spatial reference system.
    """

    __slots__ = ("members",)

    #: The class of a member.
    MEMBER_CLASS: ClassVar[type[Geometry]] = Geometry

    def __init__(self, members: Sequence[Any] = (), *, srid: int | None = None) -> None:
        super().__init__(srid)
        if isinstance(members, (str, bytes)) or not isinstance(members, Sequence):
            raise ValidationError(f"A {self.GEOMETRY_TYPE.value} is a sequence of members, got {members!r}")
        geometries = tuple(self.get_member(member) for member in members)
        if len({geometry.has_z for geometry in geometries if not geometry.is_empty}) > 1:
            raise ValidationError(f"The members of a {self.GEOMETRY_TYPE.value} mix 2 and 3 coordinates")
        self.members = geometries

    def get_member(self, member: Any) -> Geometry:
        """One member as a geometry without an SRID.

        Args:
            member: A geometry of ``MEMBER_CLASS``, or its coordinates.

        Returns:
            The member.

        Raises:
            ValidationError: The member is a geometry of another type, or wrong coordinates.
        """
        if isinstance(member, Geometry):
            if not isinstance(member, self.MEMBER_CLASS):
                raise ValidationError(
                    f"A member of a {self.GEOMETRY_TYPE.value} is a {self.MEMBER_CLASS.GEOMETRY_TYPE.value}, "
                    f"got {member!r}"
                )
            return member.with_srid(None)
        return self.MEMBER_CLASS.from_coordinates(member)

    @classmethod
    def from_coordinates(cls, coordinates: Any, srid: int | None = None) -> MultiGeometry:
        return cls(coordinates, srid=srid)

    def get_coordinates(self) -> tuple[Any, ...]:
        return tuple(member.get_coordinates() for member in self.members)

    @property
    def is_empty(self) -> bool:
        return all(member.is_empty for member in self.members)

    @property
    def has_z(self) -> bool:
        return any(member.has_z for member in self.members)

    def get_wkt_body(self) -> str:
        if not self.members:
            return "EMPTY"
        return f"({','.join(member.get_wkt_body() for member in self.members)})"
