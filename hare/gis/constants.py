from __future__ import annotations

import re

from hare.gis.enums import GeometryType
from hare.lazy_loading.lazy_pattern import LazyPattern
from hare.sql.enums import Equality

#: The SRID of WGS 84 longitude/latitude - a ``GeometryField``'s default.
DEFAULT_SRID = 4326
#: The largest SRID a spatial database takes (PostGIS's ``SRID_MAXIMUM``).
MAX_SRID = 999999
#: The coordinate dimensions a geometry may have - x/y, or x/y/z.
GEOMETRY_DIMENSIONS = frozenset({2, 3})

#: The GeoJSON ``type`` of each geometry type.
GEO_JSON_TYPE_NAMES = {
    GeometryType.POINT: "Point",
    GeometryType.LINESTRING: "LineString",
    GeometryType.POLYGON: "Polygon",
    GeometryType.MULTIPOINT: "MultiPoint",
    GeometryType.MULTILINESTRING: "MultiLineString",
    GeometryType.MULTIPOLYGON: "MultiPolygon",
    GeometryType.GEOMETRYCOLLECTION: "GeometryCollection",
}

#: The (E)WKB type code of each geometry type, without the dimension flags.
WKB_TYPE_CODES = {
    GeometryType.POINT: 1,
    GeometryType.LINESTRING: 2,
    GeometryType.POLYGON: 3,
    GeometryType.MULTIPOINT: 4,
    GeometryType.MULTILINESTRING: 5,
    GeometryType.MULTIPOLYGON: 6,
    GeometryType.GEOMETRYCOLLECTION: 7,
}
#: The EWKB flags of a type code - a z coordinate, an m coordinate, an SRID after the code.
EWKB_Z_FLAG = 0x80000000
EWKB_M_FLAG = 0x40000000
EWKB_SRID_FLAG = 0x20000000
#: ISO WKB adds these to a type code for a z, an m and both coordinates (``POINT Z`` is 1001).
ISO_WKB_Z_OFFSET = 1000
ISO_WKB_M_OFFSET = 2000
ISO_WKB_ZM_OFFSET = 3000
#: The first byte of a WKB value - big-endian (XDR) or little-endian (NDR).
WKB_BIG_ENDIAN = 0
WKB_LITTLE_ENDIAN = 1

#: A WKB value written as hex text - how PostgreSQL returns a geometry column.
HEX_WKB_PATTERN = LazyPattern(r"(?:00|01)(?:[0-9A-Fa-f]{2})+")
#: The SRID prefix of an EWKT value - ``SRID=4326;POINT(1 2)``.
EWKT_SRID_PATTERN = LazyPattern(r"\s*SRID\s*=\s*(-?\d+)\s*;", re.IGNORECASE)
#: PostGIS's text of a 2D box - ``BOX(xmin ymin,xmax ymax)``.
EXTENT_BOX_PATTERN = LazyPattern(
    r"BOX\(\s*([-+0-9.eE]+)\s+([-+0-9.eE]+)\s*,\s*([-+0-9.eE]+)\s+([-+0-9.eE]+)\s*\)", re.IGNORECASE
)
#: A ``relate`` lookup's DE-9IM pattern - nine of ``0``, ``1``, ``2``, ``T``, ``F``, ``*``.
RELATE_PATTERN = LazyPattern(r"[012TF*]{9}", re.IGNORECASE)
#: The ``Features`` flag a connection needs for the spatial lookups, functions, aggregates and the
#: paths of a geometry field.
SPATIAL_LOOKUP_REQUIRED_FEATURE = "supports_spatial"
#: The ``Features`` flag a connection needs for the lookups and functions of a geography.
GEOGRAPHY_REQUIRED_FEATURE = "supports_geography"
#: The distance lookups of a geometry field and the comparison each makes.
DISTANCE_LOOKUP_COMPARISONS = {
    "distance_lt": Equality.LT,
    "distance_lte": Equality.LTE,
    "distance_gt": Equality.GT,
    "distance_gte": Equality.GTE,
}
#: The lookup matching rows whose geometry is (or isn't) valid.
IS_VALID_LOOKUP = "isvalid"
#: The generic lookups a geometry keeps - equality and ``isnull``.
KEPT_GENERIC_GEOMETRY_LOOKUPS = ("", "not", "isnull", "not_isnull")
#: One token of a WKT value - a word, a number, a parenthesis or a comma.
WKT_TOKEN_PATTERN = LazyPattern(r"\s*(?:([A-Za-z]+)|([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)|([(),]))")

#: The module of each name the package exports - imported on first use: a dialect reading one
#: enumeration of the package doesn't load its fields, geometries and readers.
EXPORTED_MODULES = {
    "Geometry": "hare.gis.geometries",
    "GeometryCollection": "hare.gis.geometries",
    "GeometryCollectionField": "hare.gis.fields",
    "GeometryField": "hare.gis.fields",
    "GeometryType": "hare.gis.enums",
    "LineString": "hare.gis.geometries",
    "LineStringField": "hare.gis.fields",
    "MultiLineString": "hare.gis.geometries",
    "MultiLineStringField": "hare.gis.fields",
    "MultiPoint": "hare.gis.geometries",
    "MultiPointField": "hare.gis.fields",
    "MultiPolygon": "hare.gis.geometries",
    "MultiPolygonField": "hare.gis.fields",
    "Point": "hare.gis.geometries",
    "PointField": "hare.gis.fields",
    "Polygon": "hare.gis.geometries",
    "PolygonField": "hare.gis.fields",
}
