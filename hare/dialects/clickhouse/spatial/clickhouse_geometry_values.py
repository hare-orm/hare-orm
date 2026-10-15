from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from hare.dialects.clickhouse.spatial.constants import CLICKHOUSE_GEOMETRY_COLUMN_TYPES
from hare.dialects.clickhouse.types.declarations import ClickhouseTypedValue
from hare.exceptions import UnSupportedError, ValidationError
from hare.fields.data.containers.declarations import TupleValue
from hare.gis.enums import GeometryType
from hare.gis.geometries.declarations import MultiLineString, MultiPolygon
from hare.gis.geometries.line_string import LineString
from hare.gis.geometries.point import Point
from hare.gis.geometries.polygon import Polygon

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.fields.field import Field
    from hare.gis.fields.geometry_field import GeometryField
    from hare.gis.geometries.geometry import Geometry
    from hare.models import Model


class ClickhouseGeometryValues:
    """How ClickHouse stores a ``GeometryField`` - its ``Point``, ``LineString``, ``Polygon``,
    ``MultiLineString`` or ``MultiPolygon`` type: a point is a tuple of x and y, the others arrays of
    them, with no SRID (a value read takes the field's). A polygon's rings are stored in the
    orientation ClickHouse's functions read - the outer ring clockwise, the holes counter-clockwise -
    so a polygon given the other way round reads back with its rings reversed. A geometry type is never
    ``Nullable``: as a container's, a NULL line or area is written as an empty one, read back empty; a
    point, which has no empty value, holds no NULL. An area a function computes (an intersection) is read
    as a polygon or a multipolygon, as it has one or several."""

    @staticmethod
    def get_column_type(field: Field[Any]) -> str | None:
        """The ClickHouse type of the field's geometry - None for a type ClickHouse has no column of (a
        geometry of any type, points, a collection) and for x/y/z.

        Args:
            field: The geometry field.

        Returns:
            The column type.

        Raises:
            UnSupportedError: A point field is declared with ``null=True``.
        """
        geometry_field = cast("GeometryField", field)
        if geometry_field.dimensions != 2:
            return None
        geometry_type = geometry_field.get_geometry_type()
        if geometry_type == GeometryType.POINT and geometry_field.null:
            raise UnSupportedError(
                f"A point field {geometry_field.model_field_name or ''} holds no NULL on ClickHouse - a Point is a "
                "tuple, which has no empty value"
            )
        return CLICKHOUSE_GEOMETRY_COLUMN_TYPES.get(geometry_type)

    @staticmethod
    def get_ring_area(ring: Any) -> float:
        """The signed area of a ring - positive for one drawn counter-clockwise.

        Args:
            ring: The ring's points.

        Returns:
            The area.
        """
        return (
            sum(
                first[0] * second[1] - second[0] * first[1]
                for first, second in zip(ring, [*ring[1:], *ring[:1]], strict=True)
            )
            / 2
        )

    @classmethod
    def get_oriented_ring(cls, ring: Any, clockwise: bool) -> list[TupleValue]:
        """A ring's points in an orientation.

        Args:
            ring: The points.
            clockwise: Whether the ring is drawn clockwise.

        Returns:
            The points.
        """
        points = [TupleValue(point) for point in ring]
        if (cls.get_ring_area(ring) > 0) == clockwise:
            points.reverse()
        return points

    @classmethod
    def get_polygon_coordinates(cls, rings: Any) -> list[list[TupleValue]]:
        """A polygon's rings - the outer one clockwise, the holes counter-clockwise.

        Args:
            rings: The rings, the outer one first.

        Returns:
            The rings.
        """
        return [cls.get_oriented_ring(ring, clockwise=index == 0) for index, ring in enumerate(rings)]

    @classmethod
    def get_coordinates(cls, geometry: Geometry) -> Any:
        """A geometry as ClickHouse stores it.

        Args:
            geometry: The geometry, of a type ClickHouse stores.

        Returns:
            Its coordinates - tuples of x and y, in arrays.

        Raises:
            UnSupportedError: The geometry is of a type ClickHouse doesn't store, or has a z.
            ValidationError: The geometry is an empty point.
        """
        if geometry.has_z:
            raise UnSupportedError("ClickHouse stores a geometry's x and y only, not its z")
        if geometry.is_empty and geometry.GEOMETRY_TYPE == GeometryType.POINT:
            raise ValidationError("ClickHouse stores no empty point - a point is a tuple of its x and y")
        coordinates = geometry.get_coordinates()
        geometry_type = geometry.GEOMETRY_TYPE
        if geometry_type == GeometryType.POINT:
            return TupleValue(coordinates)
        if geometry_type == GeometryType.LINESTRING:
            return [TupleValue(point) for point in coordinates]
        if geometry_type == GeometryType.MULTILINESTRING:
            return [[TupleValue(point) for point in line] for line in coordinates]
        if geometry_type == GeometryType.POLYGON:
            return cls.get_polygon_coordinates(coordinates)
        if geometry_type == GeometryType.MULTIPOLYGON:
            return [cls.get_polygon_coordinates(polygon) for polygon in coordinates]
        raise UnSupportedError(f"ClickHouse stores no {geometry_type.value} - it has no type for one")

    @classmethod
    def get_typed_value(cls, geometry: Geometry) -> ClickhouseTypedValue:
        """A geometry bound with its ClickHouse type.

        Args:
            geometry: The geometry.

        Returns:
            The typed value.
        """
        return ClickhouseTypedValue(
            cls.get_coordinates(geometry), CLICKHOUSE_GEOMETRY_COLUMN_TYPES[geometry.GEOMETRY_TYPE]
        )

    @classmethod
    def to_db(cls, field: Field[Any], value: Any, instance: type[Model] | Model | None) -> Any:
        """The geometry as the column stores it - of the field's type and SRID.

        Raises:
            ValidationError: The value isn't such a geometry.
        """
        geometry_field = cast("GeometryField", field)
        geometry_field.validate(value)
        if value is None:
            # The empty line or area a NULL is written as.
            return ClickhouseTypedValue([], CLICKHOUSE_GEOMETRY_COLUMN_TYPES[geometry_field.get_geometry_type()])
        return cls.get_typed_value(geometry_field.get_checked_geometry(geometry_field.get_parsed_geometry(value)))

    @classmethod
    def to_lookup(cls, field: Field[Any], value: Any, instance: type[Model] | Model | None) -> Any:
        """The geometry an equality compares the column with.

        Raises:
            ValidationError: The value isn't a geometry.
        """
        if value is None:
            return None
        return cls.get_typed_value(cast("GeometryField", field).get_parsed_geometry(value))

    @staticmethod
    def to_python(field: Field[Any], value: Any) -> Any:
        """The geometry of a value read - in the field's SRID.

        Args:
            field: The geometry field.
            value: The coordinates the driver returned.

        Returns:
            The geometry.
        """
        if value is None:
            return None
        geometry_field = cast("GeometryField", field)
        srid = geometry_field.srid
        geometry_type = geometry_field.get_geometry_type()
        if geometry_type == GeometryType.POINT:
            return Point(*value, srid=srid)
        if geometry_type == GeometryType.LINESTRING:
            return LineString(value, srid=srid)
        if geometry_type == GeometryType.MULTILINESTRING:
            return MultiLineString(value, srid=srid)
        if not value:
            return Polygon(srid=srid) if geometry_type == GeometryType.POLYGON else MultiPolygon(srid=srid)
        # An area's rings hold points - a multipolygon's polygons hold rings.
        if isinstance(value[0][0], tuple):
            return Polygon(value[0], value[1:], srid=srid)
        return MultiPolygon([Polygon(polygon[0], polygon[1:]) for polygon in value], srid=srid)
