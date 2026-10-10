from __future__ import annotations

from hare.classes.declared_subclass import DeclaredSubclass
from hare.gis.enums import GeometryType
from hare.gis.fields.geometry_field import GeometryField

PointField = DeclaredSubclass.make(
    GeometryField,
    "PointField",
    __package__,
    """A ``GeometryField`` of points.""",
    DEFAULT_GEOMETRY_TYPE=GeometryType.POINT,
)

LineStringField = DeclaredSubclass.make(
    GeometryField,
    "LineStringField",
    __package__,
    """A ``GeometryField`` of lines.""",
    DEFAULT_GEOMETRY_TYPE=GeometryType.LINESTRING,
)

PolygonField = DeclaredSubclass.make(
    GeometryField,
    "PolygonField",
    __package__,
    """A ``GeometryField`` of polygons.""",
    DEFAULT_GEOMETRY_TYPE=GeometryType.POLYGON,
)

MultiPointField = DeclaredSubclass.make(
    GeometryField,
    "MultiPointField",
    __package__,
    """A ``GeometryField`` of multipoints.""",
    DEFAULT_GEOMETRY_TYPE=GeometryType.MULTIPOINT,
)

MultiLineStringField = DeclaredSubclass.make(
    GeometryField,
    "MultiLineStringField",
    __package__,
    """A ``GeometryField`` of multilinestrings.""",
    DEFAULT_GEOMETRY_TYPE=GeometryType.MULTILINESTRING,
)

MultiPolygonField = DeclaredSubclass.make(
    GeometryField,
    "MultiPolygonField",
    __package__,
    """A ``GeometryField`` of multipolygons.""",
    DEFAULT_GEOMETRY_TYPE=GeometryType.MULTIPOLYGON,
)

GeometryCollectionField = DeclaredSubclass.make(
    GeometryField,
    "GeometryCollectionField",
    __package__,
    """A ``GeometryField`` of geometry collections.""",
    DEFAULT_GEOMETRY_TYPE=GeometryType.GEOMETRYCOLLECTION,
)
