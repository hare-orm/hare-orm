from __future__ import annotations

from hare.dialects.sqlite.enums import SpatialiteMetadata
from hare.gis.enums import GeometryType, SpatialFunctionType, SpatialRelation

#: Points SpatiaLite's PROJ at a ``proj.db``.
SQLITE_SPATIAL_PROJ_DATABASE_SQL = "SELECT PROJ_SetDatabasePath(?)"

#: Whether the database has SpatiaLite's spatial metadata - 0 when it has none.
SQLITE_SPATIAL_METADATA_CHECK_SQL = "SELECT CheckSpatialMetaData()"

#: Creates SpatiaLite's spatial metadata, in one transaction - of each type hare creates.
SQLITE_SPATIAL_METADATA_CREATE_SQLS = {
    SpatialiteMetadata.WGS84: "SELECT InitSpatialMetadata(1, 'WGS84_ONLY')",
    SpatialiteMetadata.FULL: "SELECT InitSpatialMetadata(1)",
}

#: The SRIDs of the reference systems the spatial metadata has.
SQLITE_SPATIAL_REFERENCE_IDS_SQL = "SELECT srid FROM spatial_ref_sys"

#: The rows a spatial relation can hold for, by the bounding boxes in a column's spatial index:
#: ``{row_key}`` among the R*Tree's rows whose box meets ``{frame}``.
SPATIALITE_INDEX_PREFILTER_SQL = (
    "{row_key} IN (SELECT ROWID FROM SpatialIndex WHERE f_table_name = {table} "
    "AND f_geometry_column = {column} AND search_frame = {frame})"
)

#: A geometry's bounding box grown by a distance on every side - the frame of ``dwithin``.
SPATIALITE_EXPANDED_FRAME_SQL = "ST_Expand({geometry}, {distance})"

#: The relations no row outside the other geometry's bounding box can hold - those a spatial index
#: narrows the rows of; ``dwithin`` through the box grown by its distance.
SPATIALITE_INDEXED_RELATIONS = frozenset(
    {
        SpatialRelation.INTERSECTS,
        SpatialRelation.CONTAINS,
        SpatialRelation.CONTAINS_PROPERLY,
        SpatialRelation.WITHIN,
        SpatialRelation.COVERS,
        SpatialRelation.COVERED_BY,
        SpatialRelation.CROSSES,
        SpatialRelation.EQUALS,
        SpatialRelation.OVERLAPS,
        SpatialRelation.TOUCHES,
        SpatialRelation.BBOX_OVERLAPS,
        SpatialRelation.BBOX_CONTAINS,
        SpatialRelation.BBOX_CONTAINED,
        SpatialRelation.DWITHIN,
    }
)

#: The parts of SpatiaLite's geometry BLOB: its first byte, the byte orders, the byte ending the
#: bounding box, the byte before each member of a collection, and its last byte.
SPATIALITE_BLOB_START = 0x00

SPATIALITE_BLOB_BIG_ENDIAN = 0x00

SPATIALITE_BLOB_LITTLE_ENDIAN = 0x01

SPATIALITE_BLOB_MBR_END = 0x7C

SPATIALITE_BLOB_ENTITY = 0x69

SPATIALITE_BLOB_END = 0xFE

#: The offset of the byte ending the bounding box, and the size of the shortest BLOB.
SPATIALITE_BLOB_MBR_END_OFFSET = 38

SPATIALITE_BLOB_MIN_SIZE = 44

#: SpatiaLite adds these to a class code for a z coordinate, an m one (3000 for both), and a compressed
#: geometry.
SPATIALITE_Z_CLASS_OFFSET = 1000

SPATIALITE_M_CLASS_OFFSET = 2000

SPATIALITE_COMPRESSED_CLASS_OFFSET = 1000000

#: The column type of each geometry type - a z column adds ``Z``.
SPATIALITE_COLUMN_TYPES = {
    GeometryType.GEOMETRY: "GEOMETRY",
    GeometryType.POINT: "POINT",
    GeometryType.LINESTRING: "LINESTRING",
    GeometryType.POLYGON: "POLYGON",
    GeometryType.MULTIPOINT: "MULTIPOINT",
    GeometryType.MULTILINESTRING: "MULTILINESTRING",
    GeometryType.MULTIPOLYGON: "MULTIPOLYGON",
    GeometryType.GEOMETRYCOLLECTION: "GEOMETRYCOLLECTION",
}

#: The SpatiaLite function testing each spatial relation - ``contains_properly`` is a DE-9IM pattern,
#: SpatiaLite having no function of its own for it.
SPATIALITE_RELATION_FUNCTIONS = {
    SpatialRelation.INTERSECTS: "ST_Intersects",
    SpatialRelation.CONTAINS: "ST_Contains",
    SpatialRelation.WITHIN: "ST_Within",
    SpatialRelation.COVERS: "ST_Covers",
    SpatialRelation.COVERED_BY: "ST_CoveredBy",
    SpatialRelation.CROSSES: "ST_Crosses",
    SpatialRelation.DISJOINT: "ST_Disjoint",
    SpatialRelation.EQUALS: "ST_Equals",
    SpatialRelation.OVERLAPS: "ST_Overlaps",
    SpatialRelation.TOUCHES: "ST_Touches",
    SpatialRelation.RELATE: "ST_Relate",
    SpatialRelation.BBOX_OVERLAPS: "MbrIntersects",
    SpatialRelation.BBOX_CONTAINS: "MbrContains",
    SpatialRelation.BBOX_CONTAINED: "MbrWithin",
}

#: The DE-9IM pattern of ``contains_properly``.
SPATIALITE_CONTAINS_PROPERLY_PATTERN = "T**FF*FF*"

#: The SpatiaLite function of each spatial function.
SPATIALITE_FUNCTIONS = {
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
    SpatialFunctionType.AS_GEO_JSON: "AsGeoJSON",
    SpatialFunctionType.NUM_POINTS: "ST_NPoints",
    SpatialFunctionType.NUM_GEOMETRIES: "ST_NumGeometries",
    SpatialFunctionType.GEOMETRY_TYPE_NAME: "GeometryType",
    SpatialFunctionType.SRID: "ST_SRID",
    SpatialFunctionType.X: "ST_X",
    SpatialFunctionType.Y: "ST_Y",
    SpatialFunctionType.Z: "ST_Z",
}

#: The functions SpatiaLite measures on the ellipsoid, in meters, for a geography - its last argument
#: asks for it. Every other function SpatiaLite has no ellipsoid form of: on a geography only those
#: reading the stored longitude/latitude as they are run.
SPATIALITE_GEOGRAPHY_MEASURES = frozenset(
    {
        SpatialFunctionType.AREA,
        SpatialFunctionType.LENGTH,
        SpatialFunctionType.PERIMETER,
        SpatialFunctionType.DISTANCE,
    }
)

#: The geography functions PostGIS computes on the spheroid that SpatiaLite has no form of.
SPATIALITE_UNSUPPORTED_GEOGRAPHY_FUNCTIONS = frozenset(
    {SpatialFunctionType.CENTROID, SpatialFunctionType.BUFFER, SpatialFunctionType.INTERSECTION}
)

#: The relations a geography takes on SQLite - measured on the ellipsoid.
SPATIALITE_GEOGRAPHY_RELATIONS = frozenset({SpatialRelation.DWITHIN})

#: The SpatiaLite aggregate of each spatial aggregate.
SPATIALITE_AGGREGATES = {
    SpatialFunctionType.COLLECT: "ST_Collect",
    SpatialFunctionType.EXTENT: "Extent",
    SpatialFunctionType.UNION_AGGREGATE: "ST_Union",
    SpatialFunctionType.MAKE_LINE: "MakeLine",
}

#: The suffix SpatiaLite's ``GeometryType`` adds for a z coordinate - PostGIS adds none.
SPATIALITE_GEOMETRY_TYPE_Z_SUFFIX = " Z"
