from __future__ import annotations

from typing import TYPE_CHECKING

from hare.classes.lazy_exports import LazyExports
from hare.gis.constants import EXPORTED_MODULES

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.gis.enums import GeometryType
    from hare.gis.fields import (
        GeometryCollectionField,
        GeometryField,
        LineStringField,
        MultiLineStringField,
        MultiPointField,
        MultiPolygonField,
        PointField,
        PolygonField,
    )
    from hare.gis.geometries import (
        Geometry,
        GeometryCollection,
        LineString,
        MultiLineString,
        MultiPoint,
        MultiPolygon,
        Point,
        Polygon,
    )

__all__ = [
    "GeometryType",
    "Geometry",
    "Point",
    "LineString",
    "Polygon",
    "MultiPoint",
    "MultiLineString",
    "MultiPolygon",
    "GeometryCollection",
    "GeometryField",
    "PointField",
    "LineStringField",
    "PolygonField",
    "MultiPointField",
    "MultiLineStringField",
    "MultiPolygonField",
    "GeometryCollectionField",
]


__getattr__ = LazyExports(__name__, EXPORTED_MODULES).get
