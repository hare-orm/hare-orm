from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

from hare.exceptions import ValidationError
from hare.gis.constants import GEO_JSON_TYPE_NAMES
from hare.gis.enums import GeometryType
from hare.gis.geometries.geometry import Geometry
from hare.gis.geometries.geometry_collection import GeometryCollection
from hare.gis.readers.wkt_reader import WktReader


class GeoJsonReader:
    """Reads a geometry from a GeoJSON geometry object - a mapping, or any object with
    ``__geo_interface__`` (a shapely geometry). GeoJSON has no SRID: the geometry has none."""

    #: The geometry class of each GeoJSON type name.
    GEOMETRY_CLASSES: ClassVar[dict[str, type[Geometry]]] = {
        type_name: WktReader.GEOMETRY_CLASSES[geometry_type]
        for geometry_type, type_name in GEO_JSON_TYPE_NAMES.items()
    }

    @classmethod
    def read(cls, value: Any) -> Geometry:
        """The geometry of a GeoJSON geometry object.

        Args:
            value: The mapping, or an object with ``__geo_interface__``.

        Returns:
            The geometry.

        Raises:
            ValidationError: ``value`` isn't a GeoJSON geometry.
        """
        mapping = getattr(value, "__geo_interface__", value)
        if not isinstance(mapping, Mapping):
            raise ValidationError(f"A GeoJSON geometry is a mapping, got {value!r}")
        geometry_class = cls.GEOMETRY_CLASSES.get(mapping.get("type"))  # type: ignore[arg-type]
        if geometry_class is None:
            raise ValidationError(f"Not a GeoJSON geometry type: {mapping.get('type')!r}")
        if geometry_class.GEOMETRY_TYPE == GeometryType.GEOMETRYCOLLECTION:
            members = mapping.get("geometries")
            if not isinstance(members, list):
                raise ValidationError("A GeoJSON GeometryCollection has a list of geometries")
            return GeometryCollection([cls.read(member) for member in members])
        if "coordinates" not in mapping:
            raise ValidationError(f"A GeoJSON {mapping['type']} has coordinates")
        return geometry_class.from_coordinates(mapping["coordinates"])
