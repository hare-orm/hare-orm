from __future__ import annotations

import re

from hare.gis.enums import GeometryType, SpatialFunctionType, SpatialRelation

#: The PostGIS function testing each spatial relation.
POSTGIS_RELATION_FUNCTIONS = {
    SpatialRelation.INTERSECTS: "ST_Intersects",
    SpatialRelation.CONTAINS: "ST_Contains",
    SpatialRelation.CONTAINS_PROPERLY: "ST_ContainsProperly",
    SpatialRelation.WITHIN: "ST_Within",
    SpatialRelation.COVERS: "ST_Covers",
    SpatialRelation.COVERED_BY: "ST_CoveredBy",
    SpatialRelation.CROSSES: "ST_Crosses",
    SpatialRelation.DISJOINT: "ST_Disjoint",
    SpatialRelation.EQUALS: "ST_Equals",
    SpatialRelation.OVERLAPS: "ST_Overlaps",
    SpatialRelation.TOUCHES: "ST_Touches",
    SpatialRelation.RELATE: "ST_Relate",
    SpatialRelation.DWITHIN: "ST_DWithin",
}

#: The PostGIS operator of each bounding-box relation.
POSTGIS_BOUNDING_BOX_OPERATORS = {
    SpatialRelation.BBOX_OVERLAPS: "&&",
    SpatialRelation.BBOX_CONTAINS: "~",
    SpatialRelation.BBOX_CONTAINED: "@",
}

#: The relations PostGIS tests on a geography; the others are planar, geometry only.
POSTGIS_GEOGRAPHY_RELATIONS = frozenset(
    {
        SpatialRelation.INTERSECTS,
        SpatialRelation.COVERS,
        SpatialRelation.COVERED_BY,
        SpatialRelation.DWITHIN,
        SpatialRelation.BBOX_OVERLAPS,
    }
)

#: The PostGIS function of each spatial function.
POSTGIS_FUNCTIONS = {
    SpatialFunctionType.AREA: "ST_Area",
    SpatialFunctionType.LENGTH: "ST_Length",
    SpatialFunctionType.PERIMETER: "ST_Perimeter",
    SpatialFunctionType.DISTANCE: "ST_Distance",
    SpatialFunctionType.CENTROID: "ST_Centroid",
    SpatialFunctionType.ENVELOPE: "ST_Envelope",
    SpatialFunctionType.POINT_ON_SURFACE: "ST_PointOnSurface",
    SpatialFunctionType.BOUNDARY: "ST_Boundary",
    SpatialFunctionType.CONVEX_HULL: "ST_ConvexHull",
    SpatialFunctionType.BUFFER: "ST_Buffer",
    SpatialFunctionType.INTERSECTION: "ST_Intersection",
    SpatialFunctionType.DIFFERENCE: "ST_Difference",
    SpatialFunctionType.SYMMETRIC_DIFFERENCE: "ST_SymDifference",
    SpatialFunctionType.UNION: "ST_Union",
    SpatialFunctionType.TRANSFORM: "ST_Transform",
    SpatialFunctionType.SIMPLIFY: "ST_Simplify",
    SpatialFunctionType.MAKE_VALID: "ST_MakeValid",
    SpatialFunctionType.IS_VALID: "ST_IsValid",
    SpatialFunctionType.AS_TEXT: "ST_AsText",
    SpatialFunctionType.AS_GEO_JSON: "ST_AsGeoJSON",
    SpatialFunctionType.NUM_POINTS: "ST_NPoints",
    SpatialFunctionType.NUM_GEOMETRIES: "ST_NumGeometries",
    SpatialFunctionType.GEOMETRY_TYPE_NAME: "GeometryType",
    SpatialFunctionType.SRID: "ST_SRID",
    SpatialFunctionType.X: "ST_X",
    SpatialFunctionType.Y: "ST_Y",
    SpatialFunctionType.Z: "ST_Z",
}

#: The functions PostGIS computes on a geography itself - measured in meters on the spheroid. Any
#: other reads the geography's longitude/latitude as a geometry.
POSTGIS_GEOGRAPHY_FUNCTIONS = frozenset(
    {
        SpatialFunctionType.AREA,
        SpatialFunctionType.LENGTH,
        SpatialFunctionType.PERIMETER,
        SpatialFunctionType.DISTANCE,
        SpatialFunctionType.CENTROID,
        SpatialFunctionType.BUFFER,
        SpatialFunctionType.INTERSECTION,
        SpatialFunctionType.AS_TEXT,
        SpatialFunctionType.AS_GEO_JSON,
        SpatialFunctionType.SRID,
    }
)

#: The PostGIS aggregate of each spatial aggregate.
POSTGIS_AGGREGATES = {
    SpatialFunctionType.COLLECT: "ST_Collect",
    SpatialFunctionType.EXTENT: "ST_Extent",
    SpatialFunctionType.UNION_AGGREGATE: "ST_Union",
    SpatialFunctionType.MAKE_LINE: "ST_MakeLine",
}

#: The PostGIS type modifier of each geometry type - ``geometry(Point,4326)``.
POSTGIS_GEOMETRY_TYPE_NAMES = {
    GeometryType.GEOMETRY: "Geometry",
    GeometryType.POINT: "Point",
    GeometryType.LINESTRING: "LineString",
    GeometryType.POLYGON: "Polygon",
    GeometryType.MULTIPOINT: "MultiPoint",
    GeometryType.MULTILINESTRING: "MultiLineString",
    GeometryType.MULTIPOLYGON: "MultiPolygon",
    GeometryType.GEOMETRYCOLLECTION: "GeometryCollection",
}
#: The extension the spatial column types come from.
POSTGIS_EXTENSION = "postgis"
#: The PostGIS column types.
POSTGIS_GEOMETRY_TYPE = "geometry"
POSTGIS_GEOGRAPHY_TYPE = "geography"
#: A PostGIS column type with its type and SRID - ``geometry(PointZ,4326)``.
POSTGIS_COLUMN_TYPE_PATTERN = re.compile(
    r"(?P<column_type>geometry|geography)\((?P<type_name>[A-Za-z]+?)(?P<z>Z?)(?P<measured>M?),(?P<srid>\d+)\)",
    re.IGNORECASE,
)
#: The ``hare.gis.fields`` class of each geometry type, for a generated model.
POSTGIS_FIELD_CLASS_NAMES = {
    GeometryType.GEOMETRY: "GeometryField",
    GeometryType.POINT: "PointField",
    GeometryType.LINESTRING: "LineStringField",
    GeometryType.POLYGON: "PolygonField",
    GeometryType.MULTIPOINT: "MultiPointField",
    GeometryType.MULTILINESTRING: "MultiLineStringField",
    GeometryType.MULTIPOLYGON: "MultiPolygonField",
    GeometryType.GEOMETRYCOLLECTION: "GeometryCollectionField",
}
