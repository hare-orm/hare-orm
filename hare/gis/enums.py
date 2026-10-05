from __future__ import annotations

from enum import StrEnum


class GeometryType(StrEnum):
    """The type of geometry a ``GeometryField`` column holds - its value is the type's well-known-text
    name."""

    GEOMETRY = "GEOMETRY"
    POINT = "POINT"
    LINESTRING = "LINESTRING"
    POLYGON = "POLYGON"
    MULTIPOINT = "MULTIPOINT"
    MULTILINESTRING = "MULTILINESTRING"
    MULTIPOLYGON = "MULTIPOLYGON"
    GEOMETRYCOLLECTION = "GEOMETRYCOLLECTION"


class SpatialRelation(StrEnum):
    """How a geometry lookup relates the column's geometry to the value - the lookup's suffix."""

    INTERSECTS = "intersects"
    CONTAINS = "contains"
    CONTAINS_PROPERLY = "contains_properly"
    WITHIN = "within"
    COVERS = "covers"
    COVERED_BY = "covered_by"
    CROSSES = "crosses"
    DISJOINT = "disjoint"
    EQUALS = "equals"
    OVERLAPS = "overlaps"
    TOUCHES = "touches"
    BBOX_OVERLAPS = "bbox_overlaps"
    BBOX_CONTAINS = "bbox_contains"
    BBOX_CONTAINED = "bbox_contained"
    RELATE = "relate"
    DWITHIN = "dwithin"


class SpatialFunctionType(StrEnum):
    """A spatial SQL function, named by what it computes - each dialect writes its own SQL for it."""

    AREA = "area"
    LENGTH = "length"
    PERIMETER = "perimeter"
    DISTANCE = "distance"
    CENTROID = "centroid"
    ENVELOPE = "envelope"
    POINT_ON_SURFACE = "point_on_surface"
    BOUNDARY = "boundary"
    CONVEX_HULL = "convex_hull"
    BUFFER = "buffer"
    INTERSECTION = "intersection"
    DIFFERENCE = "difference"
    SYMMETRIC_DIFFERENCE = "symmetric_difference"
    UNION = "union"
    TRANSFORM = "transform"
    SIMPLIFY = "simplify"
    MAKE_VALID = "make_valid"
    IS_VALID = "is_valid"
    AS_TEXT = "as_text"
    AS_GEO_JSON = "as_geo_json"
    NUM_POINTS = "num_points"
    NUM_GEOMETRIES = "num_geometries"
    GEOMETRY_TYPE_NAME = "geometry_type_name"
    SRID = "srid"
    X = "x"
    Y = "y"
    Z = "z"
    COLLECT = "collect"
    EXTENT = "extent"
    UNION_AGGREGATE = "union_aggregate"
    MAKE_LINE = "make_line"
