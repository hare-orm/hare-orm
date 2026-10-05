from __future__ import annotations

from hare.classes.declared_subclass import DeclaredSubclass
from hare.gis.enums import SpatialFunctionType
from hare.gis.functions.constants import COUNT_FIELD, FLAG_FIELD, GEO_JSON_FIELD, MEASURE_FIELD, TEXT_FIELD
from hare.gis.functions.spatial_function import SpatialFunction

Area = DeclaredSubclass.make(
    SpatialFunction,
    "Area",
    __package__,
    """The area of a polygon - in square meters on a geography, in the SRID's units on a geometry.""",
    function_type=SpatialFunctionType.AREA,
    value_field=MEASURE_FIELD,
)

Length = DeclaredSubclass.make(
    SpatialFunction,
    "Length",
    __package__,
    """The length of a line - in meters on a geography, in the SRID's units on a geometry.""",
    function_type=SpatialFunctionType.LENGTH,
    value_field=MEASURE_FIELD,
)

Perimeter = DeclaredSubclass.make(
    SpatialFunction,
    "Perimeter",
    __package__,
    """The length of a polygon's rings - in meters on a geography, in the SRID's units on a geometry.""",
    function_type=SpatialFunctionType.PERIMETER,
    value_field=MEASURE_FIELD,
)

Distance = DeclaredSubclass.make(
    SpatialFunction,
    "Distance",
    __package__,
    """``Distance(geometry, other)`` - the shortest distance between two geometries, in meters on a
    geography and in the SRID's units on a geometry.""",
    function_type=SpatialFunctionType.DISTANCE,
    geometry_argument_count=2,
    value_field=MEASURE_FIELD,
)

Centroid = DeclaredSubclass.make(
    SpatialFunction,
    "Centroid",
    __package__,
    """The center of mass of a geometry, a point.""",
    function_type=SpatialFunctionType.CENTROID,
)

Envelope = DeclaredSubclass.make(
    SpatialFunction,
    "Envelope",
    __package__,
    """The bounding box of a geometry, a polygon (a point or a line for a degenerate box).""",
    function_type=SpatialFunctionType.ENVELOPE,
)

PointOnSurface = DeclaredSubclass.make(
    SpatialFunction,
    "PointOnSurface",
    __package__,
    """A point guaranteed to lie on the geometry.""",
    function_type=SpatialFunctionType.POINT_ON_SURFACE,
)

Boundary = DeclaredSubclass.make(
    SpatialFunction,
    "Boundary",
    __package__,
    """The boundary of a geometry - a polygon's rings, a line's end points.""",
    function_type=SpatialFunctionType.BOUNDARY,
)

ConvexHull = DeclaredSubclass.make(
    SpatialFunction,
    "ConvexHull",
    __package__,
    """The smallest convex polygon holding the geometry.""",
    function_type=SpatialFunctionType.CONVEX_HULL,
)

Intersection = DeclaredSubclass.make(
    SpatialFunction,
    "Intersection",
    __package__,
    """``Intersection(geometry, other)`` - the part the two geometries share.""",
    function_type=SpatialFunctionType.INTERSECTION,
    geometry_argument_count=2,
)

Difference = DeclaredSubclass.make(
    SpatialFunction,
    "Difference",
    __package__,
    """``Difference(geometry, other)`` - the part of the geometry outside the other.""",
    function_type=SpatialFunctionType.DIFFERENCE,
    geometry_argument_count=2,
)

SymmetricDifference = DeclaredSubclass.make(
    SpatialFunction,
    "SymmetricDifference",
    __package__,
    """``SymmetricDifference(geometry, other)`` - the parts of the two geometries the other doesn't
    cover.""",
    function_type=SpatialFunctionType.SYMMETRIC_DIFFERENCE,
    geometry_argument_count=2,
)

Union = DeclaredSubclass.make(
    SpatialFunction,
    "Union",
    __package__,
    """``Union(geometry, other)`` - the two geometries merged; ``UnionAggregate`` merges a group's.""",
    function_type=SpatialFunctionType.UNION,
    geometry_argument_count=2,
)

MakeValid = DeclaredSubclass.make(
    SpatialFunction,
    "MakeValid",
    __package__,
    """A valid geometry covering an invalid one's points - a valid one is returned as it is.""",
    function_type=SpatialFunctionType.MAKE_VALID,
)

IsValid = DeclaredSubclass.make(
    SpatialFunction,
    "IsValid",
    __package__,
    """Whether the geometry is valid - rings closed, not self-intersecting, ...""",
    function_type=SpatialFunctionType.IS_VALID,
    value_field=FLAG_FIELD,
)

AsText = DeclaredSubclass.make(
    SpatialFunction,
    "AsText",
    __package__,
    """The geometry's well-known text, without the SRID.""",
    function_type=SpatialFunctionType.AS_TEXT,
    value_field=TEXT_FIELD,
)

AsGeoJSON = DeclaredSubclass.make(
    SpatialFunction,
    "AsGeoJSON",
    __package__,
    """The geometry as a GeoJSON geometry, read as a ``dict``.""",
    function_type=SpatialFunctionType.AS_GEO_JSON,
    value_field=GEO_JSON_FIELD,
)

NumPoints = DeclaredSubclass.make(
    SpatialFunction,
    "NumPoints",
    __package__,
    """The number of points of a line.""",
    function_type=SpatialFunctionType.NUM_POINTS,
    value_field=COUNT_FIELD,
)

NumGeometries = DeclaredSubclass.make(
    SpatialFunction,
    "NumGeometries",
    __package__,
    """The number of members of a multi geometry or collection - 1 for a single geometry.""",
    function_type=SpatialFunctionType.NUM_GEOMETRIES,
    value_field=COUNT_FIELD,
)

GeometryTypeName = DeclaredSubclass.make(
    SpatialFunction,
    "GeometryTypeName",
    __package__,
    """The geometry's type as text - ``POINT``, ``POLYGON``, ...""",
    function_type=SpatialFunctionType.GEOMETRY_TYPE_NAME,
    value_field=TEXT_FIELD,
)
