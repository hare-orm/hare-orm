from __future__ import annotations

from collections.abc import Sequence
from typing import Any, ClassVar, Self

from hare.exceptions import ValidationError
from hare.gis.constants import GEO_JSON_TYPE_NAMES, MAX_SRID
from hare.gis.enums import GeometryType
from hare.numbers.finite_numbers import FiniteNumbers


class Geometry:
    """Base of the geometry values a ``GeometryField`` holds - immutable, compared by type, SRID and
    coordinates. A geometry is written as its (E)WKT (``wkt``/``ewkt``) and is a GeoJSON geometry
    through ``__geo_interface__``, which shapely and other libraries read.

    Args:
        srid: The spatial reference system of the coordinates, None when not given - a field then
            gives its own.
    """

    __slots__ = ("srid",)

    #: The geometry's type.
    GEOMETRY_TYPE: ClassVar[GeometryType] = GeometryType.GEOMETRY

    def __init__(self, srid: int | None = None) -> None:
        self.srid = self.get_validated_srid(srid)

    @staticmethod
    def get_validated_srid(srid: Any) -> int | None:
        """An SRID - None, or an ``int`` from 0 to ``MAX_SRID`` (0 is "unknown", read back as None).

        Args:
            srid: The SRID.

        Returns:
            The SRID, None for none or 0.

        Raises:
            ValidationError: ``srid`` isn't such an ``int``.
        """
        if srid is None:
            return None
        if type(srid) is not int or not 0 <= srid <= MAX_SRID:
            raise ValidationError(f"An SRID is an int from 0 to {MAX_SRID}, got {srid!r}")
        return srid or None

    @staticmethod
    def get_coordinate(value: Any) -> tuple[float, ...]:
        """One position - a ``Point`` or a sequence of two or three finite numbers.

        Args:
            value: The position.

        Returns:
            Its coordinates as floats.

        Raises:
            ValidationError: ``value`` isn't such a position.
        """
        if isinstance(value, Geometry):
            coordinates = value.get_coordinates()
            if value.GEOMETRY_TYPE != GeometryType.POINT or not coordinates:
                raise ValidationError(f"A position is a non-empty point or a sequence of numbers, got {value!r}")
            return coordinates
        if isinstance(value, (str, bytes)) or not isinstance(value, Sequence) or len(value) not in {2, 3}:
            raise ValidationError(f"A position is a sequence of 2 or 3 numbers, got {value!r}")
        coordinates_list = []
        for number in value:
            if not FiniteNumbers.is_finite_number(number):
                raise ValidationError(f"A coordinate is a finite number, got {number!r} in {value!r}")
            coordinates_list.append(float(number))
        return tuple(coordinates_list)

    @staticmethod
    def get_same_dimension_coordinates(values: Sequence[Any], name: str) -> tuple[tuple[float, ...], ...]:
        """The positions of a line or ring, all with the same number of coordinates.

        Args:
            values: The positions.
            name: What the positions make, for the error message.

        Returns:
            The positions.

        Raises:
            ValidationError: A position is wrong, or they mix 2 and 3 coordinates.
        """
        if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
            raise ValidationError(f"A {name} is a sequence of positions, got {values!r}")
        positions = tuple(Geometry.get_coordinate(value) for value in values)
        if len({len(position) for position in positions}) > 1:
            raise ValidationError(f"The positions of a {name} mix 2 and 3 coordinates")
        return positions

    @staticmethod
    def format_number(number: float) -> str:
        """A coordinate in WKT - the shortest text reading back as the same float, without ``.0``."""
        text = repr(number)
        return text.removesuffix(".0")

    @staticmethod
    def format_position(position: tuple[float, ...]) -> str:
        """A position in WKT - its coordinates separated by spaces."""
        return " ".join(Geometry.format_number(number) for number in position)

    @staticmethod
    def format_positions(positions: Sequence[tuple[float, ...]]) -> str:
        """A parenthesized list of positions in WKT."""
        return f"({','.join(Geometry.format_position(position) for position in positions)})"

    @classmethod
    def from_coordinates(cls, coordinates: Any, srid: int | None = None) -> Self:
        """The geometry of coordinates nested as GeoJSON nests them.

        Args:
            coordinates: The coordinates.
            srid: The spatial reference system.

        Returns:
            The geometry.

        Raises:
            ValidationError: The coordinates aren't of the geometry's shape.
        """
        raise NotImplementedError

    def get_coordinates(self) -> Any:
        """The geometry's coordinates as nested tuples, as GeoJSON nests them.

        Returns:
            The coordinates.
        """
        raise NotImplementedError

    def get_wkt_body(self) -> str:
        """The WKT after the type name - ``EMPTY`` or the parenthesized coordinates.

        Returns:
            The text.
        """
        raise NotImplementedError

    @property
    def is_empty(self) -> bool:
        """Whether the geometry holds no position."""
        return not self.get_coordinates()

    @property
    def has_z(self) -> bool:
        """Whether the positions have a z coordinate."""
        raise NotImplementedError

    @property
    def wkt(self) -> str:
        """The well-known text of the geometry, without its SRID - ``POINT Z (1 2 3)``."""
        dimension_text = " Z" if self.has_z else ""
        return f"{self.GEOMETRY_TYPE.value}{dimension_text} {self.get_wkt_body()}"

    @property
    def ewkt(self) -> str:
        """The well-known text with the SRID before it, when the geometry has one -
        ``SRID=4326;POINT (1 2)``."""
        return self.wkt if self.srid is None else f"SRID={self.srid};{self.wkt}"

    @property
    def __geo_interface__(self) -> dict[str, Any]:
        """The geometry as a GeoJSON geometry object."""
        return {"type": GEO_JSON_TYPE_NAMES[self.GEOMETRY_TYPE], "coordinates": self.get_geo_json_coordinates()}

    def get_geo_json_coordinates(self) -> Any:
        """The coordinates as GeoJSON lists.

        Returns:
            The nested lists.
        """
        return self.get_lists(self.get_coordinates())

    @staticmethod
    def get_lists(value: Any) -> Any:
        """Nested tuples as nested lists, the numbers kept."""
        if isinstance(value, tuple):
            return [Geometry.get_lists(item) for item in value]
        return value

    @classmethod
    def get_validated_geometry(cls, value: Any) -> Geometry:
        """A value as a geometry of this class - for a pydantic model: a geometry, a GeoJSON
        geometry, WKT/EWKT or (E)WKB.

        Args:
            value: The value.

        Returns:
            The geometry.

        Raises:
            ValidationError: ``value`` isn't a geometry of this class.
        """
        # Local import: the readers import the geometry classes.
        from hare.gis.readers.ewkb_reader import EwkbReader
        from hare.gis.readers.geo_json_reader import GeoJsonReader
        from hare.gis.readers.wkt_reader import WktReader

        if isinstance(value, Geometry):
            geometry = value
        elif isinstance(value, (bytes, bytearray, memoryview)):
            geometry = EwkbReader.read(value)
        elif isinstance(value, str):
            geometry = WktReader.read(value)
        else:
            geometry = GeoJsonReader.read(value)
        if not isinstance(geometry, cls):
            raise ValidationError(f"Expected a {cls.GEOMETRY_TYPE.value}, got a {geometry.GEOMETRY_TYPE.value}")
        return geometry

    @staticmethod
    def get_geo_json(geometry: Geometry) -> dict[str, Any]:
        """A geometry as its GeoJSON geometry object - how a pydantic model serializes it."""
        return geometry.__geo_interface__

    @classmethod
    def __get_pydantic_core_schema__(cls, source: Any, handler: Any) -> Any:
        # Local import: pydantic is optional.
        from pydantic_core import core_schema

        return core_schema.no_info_plain_validator_function(
            cls.get_validated_geometry,
            serialization=core_schema.plain_serializer_function_ser_schema(cls.get_geo_json),
        )

    @classmethod
    def __get_pydantic_json_schema__(cls, schema: Any, handler: Any) -> dict[str, Any]:
        return {
            "type": "object",
            "description": "A GeoJSON geometry",
            "properties": {"type": {"type": "string"}, "coordinates": {"type": "array"}},
            "required": ["type"],
        }

    def with_srid(self, srid: int | None) -> Self:
        """A copy of the geometry with another SRID - the coordinates aren't transformed.

        Args:
            srid: The SRID.

        Returns:
            The copy.
        """
        copy = object.__new__(type(self))
        for slot_class in type(self).__mro__:
            for name in getattr(slot_class, "__slots__", ()):
                object.__setattr__(copy, name, getattr(self, name))
        object.__setattr__(copy, "srid", self.get_validated_srid(srid))
        return copy

    def __setattr__(self, name: str, value: Any) -> None:
        if hasattr(self, name):
            raise AttributeError(f"{type(self).__name__} is immutable")
        object.__setattr__(self, name, value)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Geometry):
            return NotImplemented
        return (
            type(self) is type(other) and self.srid == other.srid and self.get_coordinates() == other.get_coordinates()
        )

    def __hash__(self) -> int:
        return hash((type(self), self.srid, self.get_coordinates()))

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.ewkt!r})"
