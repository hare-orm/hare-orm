from __future__ import annotations

from typing import Any, ClassVar

from hare.exceptions import ValidationError
from hare.gis.constants import EWKT_SRID_PATTERN, WKT_TOKEN_PATTERN
from hare.gis.enums import GeometryType
from hare.gis.geometries.declarations import MultiLineString, MultiPoint, MultiPolygon
from hare.gis.geometries.geometry import Geometry
from hare.gis.geometries.geometry_collection import GeometryCollection
from hare.gis.geometries.line_string import LineString
from hare.gis.geometries.point import Point
from hare.gis.geometries.polygon import Polygon


class WktReader:
    """Reads a geometry from WKT, or from EWKT with an ``SRID=...;`` prefix -
    ``SRID=4326;POLYGON((0 0,1 0,1 1,0 0))``. Measured (m) coordinates are refused.

    Args:
        text: The text after the SRID prefix.
    """

    #: The class of each geometry type.
    GEOMETRY_CLASSES: ClassVar[dict[GeometryType, type[Geometry]]] = {
        GeometryType.POINT: Point,
        GeometryType.LINESTRING: LineString,
        GeometryType.POLYGON: Polygon,
        GeometryType.MULTIPOINT: MultiPoint,
        GeometryType.MULTILINESTRING: MultiLineString,
        GeometryType.MULTIPOLYGON: MultiPolygon,
        GeometryType.GEOMETRYCOLLECTION: GeometryCollection,
    }

    def __init__(self, text: str) -> None:
        self.tokens = self.get_tokens(text)
        self.position = 0

    @staticmethod
    def get_tokens(text: str) -> list[str]:
        """The words, numbers, parentheses and commas of a WKT text.

        Args:
            text: The text.

        Returns:
            The tokens - words upper-cased.

        Raises:
            ValueError: The text holds a character WKT doesn't have.
        """
        tokens = []
        index = 0
        while index < len(text):
            if text[index:].strip() == "":
                break
            match = WKT_TOKEN_PATTERN.match(text, index)
            if match is None:
                raise ValueError(f"unexpected {text[index:].strip()[:20]!r}")
            word, number, punctuation = match.groups()
            tokens.append(word.upper() if word else number or punctuation)
            index = match.end()
        return tokens

    @classmethod
    def read(cls, text: str) -> Geometry:
        """The geometry of a WKT or EWKT text.

        Args:
            text: The text.

        Returns:
            The geometry.

        Raises:
            ValidationError: ``text`` isn't a geometry's WKT.
        """
        srid = None
        srid_match = EWKT_SRID_PATTERN.match(text)
        if srid_match is not None:
            srid = int(srid_match.group(1))
            text = text[srid_match.end() :]
        try:
            reader = cls(text)
            geometry = reader.read_geometry()
            if reader.position != len(reader.tokens):
                raise ValueError(f"unexpected {reader.tokens[reader.position]!r} after the geometry")
            return geometry.with_srid(srid) if srid is not None else geometry
        except (ValueError, IndexError) as error:
            raise ValidationError(f"Not a WKT geometry: {error}") from None

    def take(self) -> str:
        """The next token, moving past it.

        Raises:
            ValueError: There is no more token.
        """
        if self.position >= len(self.tokens):
            raise ValueError("the text ends too early")
        token = self.tokens[self.position]
        self.position += 1
        return token

    def peek(self) -> str | None:
        """The next token, None at the end."""
        return self.tokens[self.position] if self.position < len(self.tokens) else None

    def expect(self, token: str) -> None:
        """Moves past ``token``.

        Raises:
            ValueError: The next token is another.
        """
        found = self.take()
        if found != token:
            raise ValueError(f"expected {token!r}, got {found!r}")

    def read_geometry(self) -> Geometry:
        """Reads a tagged geometry - its type name, dimension and body.

        Raises:
            ValueError: The tokens aren't a geometry.
        """
        type_name = self.take()
        try:
            geometry_type = GeometryType(type_name)
        except ValueError:
            raise ValueError(f"unknown geometry type {type_name!r}") from None
        if geometry_type == GeometryType.GEOMETRY:
            raise ValueError("GEOMETRY is not a geometry's type")
        if self.peek() in {"M", "ZM"}:
            raise ValueError("measured (M) coordinates are not supported")
        if self.peek() == "Z":
            self.take()
        geometry_class = self.GEOMETRY_CLASSES[geometry_type]
        if self.peek() == "EMPTY":
            self.take()
            return geometry_class.from_coordinates(())
        if geometry_type == GeometryType.GEOMETRYCOLLECTION:
            return GeometryCollection(self.read_list(self.read_geometry))
        return geometry_class.from_coordinates(self.read_body(geometry_type))

    def read_list(self, read_item: Any) -> list[Any]:
        """Reads a parenthesized, comma-separated list.

        Args:
            read_item: Reads one item.

        Returns:
            The items.
        """
        self.expect("(")
        items = [read_item()]
        while self.peek() == ",":
            self.take()
            items.append(read_item())
        self.expect(")")
        return items

    def read_position(self) -> tuple[float, ...]:
        """Reads two or three numbers.

        Raises:
            ValueError: A token isn't a number.
        """
        numbers = []
        while self.peek() not in {None, ",", ")", "("}:
            numbers.append(float(self.take()))
        if len(numbers) not in {2, 3}:
            raise ValueError(f"a position has 2 or 3 numbers, got {len(numbers)}")
        return tuple(numbers)

    def read_point_member(self) -> Any:
        """Reads a member of a MULTIPOINT - ``(1 2)``, a bare ``1 2`` or ``EMPTY``."""
        if self.peek() == "EMPTY":
            self.take()
            return ()
        if self.peek() == "(":
            self.take()
            position = self.read_position()
            self.expect(")")
            return position
        return self.read_position()

    def read_empty_or(self, read_body: Any) -> Any:
        """``EMPTY`` as empty coordinates, else ``read_body()``."""
        if self.peek() == "EMPTY":
            self.take()
            return ()
        return read_body()

    def read_body(self, geometry_type: GeometryType) -> Any:
        """Reads the parenthesized coordinates of a geometry type.

        Args:
            geometry_type: The type.

        Returns:
            The coordinates, nested as GeoJSON nests them.
        """
        if geometry_type == GeometryType.POINT:
            self.expect("(")
            position = self.read_position()
            self.expect(")")
            return position
        if geometry_type == GeometryType.LINESTRING:
            return self.read_list(self.read_position)
        if geometry_type == GeometryType.POLYGON:
            return self.read_list(lambda: self.read_list(self.read_position))
        if geometry_type == GeometryType.MULTIPOINT:
            return self.read_list(self.read_point_member)
        if geometry_type == GeometryType.MULTILINESTRING:
            return self.read_list(lambda: self.read_empty_or(lambda: self.read_list(self.read_position)))
        return self.read_list(
            lambda: self.read_empty_or(lambda: self.read_list(lambda: self.read_list(self.read_position)))
        )
