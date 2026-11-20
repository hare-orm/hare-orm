from __future__ import annotations

from hare.gis.constants import WKB_TYPE_CODES
from hare.gis.enums import GeometryType
from hare.gis.geometries.declarations import MultiLineString, MultiPoint, MultiPolygon
from hare.gis.geometries.geometry import Geometry
from hare.gis.geometries.geometry_collection import GeometryCollection
from hare.gis.geometries.line_string import LineString
from hare.gis.geometries.multi_geometry import MultiGeometry
from hare.gis.geometries.point import Point
from hare.gis.geometries.polygon import Polygon

#: The class of each geometry type accepted by text and GeoJSON readers.
GEOMETRY_CLASSES: dict[GeometryType, type[Geometry]] = {
    GeometryType.POINT: Point,
    GeometryType.LINESTRING: LineString,
    GeometryType.POLYGON: Polygon,
    GeometryType.MULTIPOINT: MultiPoint,
    GeometryType.MULTILINESTRING: MultiLineString,
    GeometryType.MULTIPOLYGON: MultiPolygon,
    GeometryType.GEOMETRYCOLLECTION: GeometryCollection,
}

#: The geometry type of each type code of WKB and of the spatial BLOBs a dialect reads.
GEOMETRY_TYPES_BY_CODE = {code: geometry_type for geometry_type, code in WKB_TYPE_CODES.items()}
#: The class of each multi geometry type a reader builds - any other collection is a GeometryCollection.
COLLECTION_CLASSES: dict[GeometryType, type[MultiGeometry]] = {
    GeometryType.MULTIPOINT: MultiPoint,
    GeometryType.MULTILINESTRING: MultiLineString,
    GeometryType.MULTIPOLYGON: MultiPolygon,
}
