from __future__ import annotations

from hare.gis.enums import GeometryType, SpatialFunctionType, SpatialRelation

#: The ClickHouse type of each type of geometry it stores - x/y only, without an SRID.
CLICKHOUSE_GEOMETRY_COLUMN_TYPES = {
    GeometryType.POINT: "Point",
    GeometryType.LINESTRING: "LineString",
    GeometryType.POLYGON: "Polygon",
    GeometryType.MULTILINESTRING: "MultiLineString",
    GeometryType.MULTIPOLYGON: "MultiPolygon",
}
#: The types of geometry with an area - a polygon's functions take them.
CLICKHOUSE_AREA_GEOMETRY_TYPES = frozenset({GeometryType.POLYGON, GeometryType.MULTIPOLYGON})
#: The radius of the sphere a geography's spherical measures are taken on, in meters - ClickHouse
#: measures on a sphere of radius one (WGS 84's mean radius).
CLICKHOUSE_EARTH_RADIUS_METERS = 6_371_008.8
#: The relations of a point and an area ClickHouse tests - a point on the area's border counts as in it.
CLICKHOUSE_POINT_IN_AREA_RELATIONS = frozenset(
    {
        SpatialRelation.INTERSECTS,
        SpatialRelation.WITHIN,
        SpatialRelation.COVERED_BY,
        SpatialRelation.CONTAINS,
        SpatialRelation.COVERS,
        SpatialRelation.DISJOINT,
    }
)
#: The relations whose left geometry is in the right one - the others have it the other way.
CLICKHOUSE_LEFT_INSIDE_RELATIONS = frozenset({SpatialRelation.WITHIN, SpatialRelation.COVERED_BY})
#: The relations of two areas ClickHouse tests -> its planar and its spherical function (None: planar
#: only).
CLICKHOUSE_AREA_RELATION_FUNCTIONS = {
    SpatialRelation.INTERSECTS: ("polygonsIntersectCartesian", "polygonsIntersectSpherical"),
    SpatialRelation.WITHIN: ("polygonsWithinCartesian", "polygonsWithinSpherical"),
    SpatialRelation.COVERED_BY: ("polygonsWithinCartesian", "polygonsWithinSpherical"),
    SpatialRelation.CONTAINS: ("polygonsWithinCartesian", "polygonsWithinSpherical"),
    SpatialRelation.COVERS: ("polygonsWithinCartesian", "polygonsWithinSpherical"),
    SpatialRelation.EQUALS: ("polygonsEqualsCartesian", None),
    SpatialRelation.DISJOINT: ("polygonsIntersectCartesian", "polygonsIntersectSpherical"),
}
#: The functions of areas -> their planar and spherical function (None: planar only), and whether a
#: spherical result is in radians (multiplied by the radius once) or square radians (twice).
CLICKHOUSE_AREA_FUNCTIONS = {
    SpatialFunctionType.AREA: ("polygonAreaCartesian", "polygonAreaSpherical", 2),
    SpatialFunctionType.PERIMETER: ("polygonPerimeterCartesian", "polygonPerimeterSpherical", 1),
    SpatialFunctionType.CONVEX_HULL: ("polygonConvexHullCartesian", None, 0),
    SpatialFunctionType.INTERSECTION: ("polygonsIntersectionCartesian", "polygonsIntersectionSpherical", 0),
    SpatialFunctionType.UNION: ("polygonsUnionCartesian", "polygonsUnionSpherical", 0),
    SpatialFunctionType.SYMMETRIC_DIFFERENCE: ("polygonsSymDifferenceCartesian", "polygonsSymDifferenceSpherical", 0),
}
#: The distance of two areas - planar, and spherical in radians.
CLICKHOUSE_AREA_DISTANCE_FUNCTIONS = ("polygonsDistanceCartesian", "polygonsDistanceSpherical")
#: The distance of two points - planar, and on the WGS 84 ellipsoid in meters (longitude, latitude).
CLICKHOUSE_POINT_DISTANCE_FUNCTIONS = ("L2Distance", "geoDistance")
#: Whether a point is in an area (its border included).
CLICKHOUSE_POINT_IN_POLYGON_FUNCTION = "pointInPolygon"
#: The function writing a geometry as well-known text.
CLICKHOUSE_AS_TEXT_FUNCTION = "wkt"
#: The coordinate each path segment reads - a point's element.
CLICKHOUSE_COORDINATE_ELEMENTS = {SpatialFunctionType.X: 1, SpatialFunctionType.Y: 2}
