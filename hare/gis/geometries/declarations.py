from __future__ import annotations

from hare.classes.declared_subclass import DeclaredSubclass
from hare.gis.enums import GeometryType
from hare.gis.geometries.line_string import LineString
from hare.gis.geometries.multi_geometry import MultiGeometry
from hare.gis.geometries.point import Point
from hare.gis.geometries.polygon import Polygon

MultiPoint = DeclaredSubclass.make(
    MultiGeometry,
    "MultiPoint",
    __package__,
    """Points - ``MultiPoint([(0, 0), Point(1, 1)])``.""",
    __slots__=(),
    GEOMETRY_TYPE=GeometryType.MULTIPOINT,
    MEMBER_CLASS=Point,
)

MultiLineString = DeclaredSubclass.make(
    MultiGeometry,
    "MultiLineString",
    __package__,
    """Lines - ``MultiLineString([[(0, 0), (1, 1)], [(2, 2), (3, 3)]])``.""",
    __slots__=(),
    GEOMETRY_TYPE=GeometryType.MULTILINESTRING,
    MEMBER_CLASS=LineString,
)

MultiPolygon = DeclaredSubclass.make(
    MultiGeometry,
    "MultiPolygon",
    __package__,
    """Polygons - each a ``Polygon`` or its rings (``[exterior, *holes]``).""",
    __slots__=(),
    GEOMETRY_TYPE=GeometryType.MULTIPOLYGON,
    MEMBER_CLASS=Polygon,
)
