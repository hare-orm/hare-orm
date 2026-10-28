from __future__ import annotations

from collections.abc import Callable, Mapping
from functools import partial
from typing import TYPE_CHECKING, Any, ClassVar

from hare.classes.value_descriptions import ValueDescriptions
from hare.exceptions import ConfigurationError, ValidationError
from hare.fields.data.constants import GEOMETRY_VALUE_TYPE
from hare.fields.data.numeric.float_field import FloatField
from hare.fields.data.numeric.int_field import IntField
from hare.fields.field import Field
from hare.gis.constants import (
    DEFAULT_SRID,
    DISTANCE_LOOKUP_COMPARISONS,
    GEOGRAPHY_LATITUDE_LIMIT,
    GEOGRAPHY_LONGITUDE_LIMIT,
    GEOMETRY_DIMENSIONS,
    HEX_WKB_PATTERN,
    MAX_SRID,
    SPATIAL_LOOKUP_REQUIRED_FEATURE,
)
from hare.gis.enums import GeometryType, SpatialFunctionType, SpatialRelation
from hare.gis.geometries.geometry import Geometry
from hare.gis.readers.ewkb_reader import EwkbReader
from hare.gis.readers.geo_json_reader import GeoJsonReader
from hare.gis.readers.wkt_reader import WktReader
from hare.gis.spatial_features import SpatialFeatures
from hare.query.enums import LookupValueShape

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.models import Model
    from hare.query.filters.lookups.field_lookup import FieldLookup
    from hare.sql.terms.term import Term


class GeometryField(Field[Geometry]):
    """A spatial column - a ``geometry`` (planar coordinates) or, with ``geography=True``, a
    ``geography`` (longitude/latitude on the spheroid, measured in meters). The Python value is a
    ``Geometry`` (``Point``, ``Polygon``, ...); WKT/EWKT text, (E)WKB bytes or hex, a GeoJSON mapping
    and any object with ``__geo_interface__`` (a shapely geometry) are taken too. A geometry without
    an SRID gets the field's. It exists on the dialects with spatial columns; where a dialect
    needs an extension for them, the migration autodetector adds its ``CreateExtension``
    wherever the field is used.

    Args:
        geometry_type: The type of geometry the column holds - any (``GEOMETRY``) by default.
        srid: The spatial reference system - 4326 (WGS 84 longitude/latitude) by default.
        geography: Whether the column is a geography.
        dimensions: 2 for x/y, 3 for x/y/z.

    Raises:
        ConfigurationError: An argument is of the wrong type or out of range.
    """

    argument_value_type = GEOMETRY_VALUE_TYPE

    COLUMN_TYPE_FROM_DIALECT = True

    field_type = Geometry
    path_required_feature = SPATIAL_LOOKUP_REQUIRED_FEATURE

    #: The type of geometry a field of this class holds when ``geometry_type`` isn't given.
    DEFAULT_GEOMETRY_TYPE: ClassVar[GeometryType] = GeometryType.GEOMETRY

    #: The fields of the values a path reads - a coordinate and the SRID.
    COORDINATE_FIELD: ClassVar[FloatField[Any]] = FloatField()
    SRID_FIELD: ClassVar[IntField[Any]] = IntField()

    #: The spatial function each path segment reads.
    PATH_FUNCTION_TYPES: ClassVar[dict[str, SpatialFunctionType]] = {
        "x": SpatialFunctionType.X,
        "y": SpatialFunctionType.Y,
        "z": SpatialFunctionType.Z,
        "srid": SpatialFunctionType.SRID,
    }

    def __init__(
        self,
        geometry_type: GeometryType | str | None = None,
        *,
        srid: int = DEFAULT_SRID,
        geography: bool = False,
        dimensions: int = 2,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        if geometry_type is not None:
            try:
                geometry_type = GeometryType(geometry_type)
            except ValueError:
                raise ConfigurationError(
                    f"geometry_type must be one of {[member.value for member in GeometryType]}, got {geometry_type!r}"
                ) from None
        if type(srid) is not int or not 1 <= srid <= MAX_SRID:
            raise ConfigurationError(
                f"srid must be an int from 1 to {MAX_SRID}, got {ValueDescriptions.get_shown_value(srid)}"
            )
        if type(dimensions) is not int or dimensions not in GEOMETRY_DIMENSIONS:
            raise ConfigurationError(f"dimensions must be 2 or 3, got {dimensions!r}")
        self.geometry_type = geometry_type
        self.srid = srid
        self.geography = geography
        self.dimensions = dimensions

    def get_json_value(self, value: Any) -> Any:
        """The JSON form of a value of the field - its GeoJSON geometry object, which the field reads back.

        Args:
            value: A value of the field, None included.

        Returns:
            The value's JSON form.

        Raises:
            TypeError: A value inside it has no JSON form.
        """
        geometry = self.to_python(value)
        if geometry is None:
            return None
        return Geometry.get_geo_json(geometry)

    def get_geometry_type(self) -> GeometryType:
        """The type of geometry the column holds."""
        return self.geometry_type or self.DEFAULT_GEOMETRY_TYPE

    def get_python_type(self) -> Any:
        return Geometry

    def get_parsed_geometry(self, value: Any) -> Geometry:
        """The geometry of a value in any form the field takes, with the field's SRID when it has none.

        Args:
            value: A ``Geometry``, WKT/EWKT, (E)WKB bytes or hex, or a GeoJSON geometry.

        Returns:
            The geometry.

        Raises:
            ValidationError: ``value`` is none of those.
        """
        validation_error = None
        try:
            if isinstance(value, Geometry):
                geometry = value
            elif isinstance(value, str):
                geometry = EwkbReader.read(value) if HEX_WKB_PATTERN.fullmatch(value) else WktReader.read(value)
            elif isinstance(value, (bytes, bytearray, memoryview)):
                geometry = EwkbReader.read(value)
            elif isinstance(value, Mapping) or hasattr(value, "__geo_interface__"):
                geometry = GeoJsonReader.read(value)
            else:
                raise ValidationError(
                    f"expected a geometry, WKT, WKB or a GeoJSON geometry, got {type(value).__name__}"
                )
        except ValidationError as error:
            validation_error = self.get_validation_error(error, value, f"{self.model_field_name}: {error}")
        if validation_error is not None:
            raise validation_error
        return geometry if geometry.srid is not None else geometry.with_srid(self.srid)

    def get_checked_geometry(self, geometry: Geometry) -> Geometry:
        """A geometry the column takes as it is - of its type, dimensions and SRID.

        Args:
            geometry: The geometry.

        Returns:
            The geometry.

        Raises:
            ValidationError: The geometry is of another type, has other dimensions or another SRID.
        """
        geometry_type = self.get_geometry_type()
        if geometry_type != GeometryType.GEOMETRY and geometry_type != geometry.GEOMETRY_TYPE:
            raise ValidationError(
                f"{self.model_field_name}: expected a {geometry_type.value}, got a {geometry.GEOMETRY_TYPE.value}"
            )
        if not geometry.is_empty and geometry.has_z != (self.dimensions == 3):
            raise ValidationError(
                f"{self.model_field_name}: expected {self.dimensions} coordinates, got {3 if geometry.has_z else 2}"
            )
        self.raise_if_other_srid(geometry)
        if self.geography and not geometry.is_empty:
            self.raise_if_off_the_globe(geometry)
        return geometry

    def raise_if_other_srid(self, geometry: Geometry) -> None:
        """Refuses a geometry of another SRID than the column's.

        Args:
            geometry: The geometry.

        Raises:
            ValidationError: The geometry has another SRID.
        """
        if geometry.srid != self.srid:
            raise ValidationError(
                f"{self.model_field_name}: the geometry's SRID {geometry.srid} isn't the column's {self.srid}"
            )

    def raise_if_off_the_globe(self, geometry: Geometry) -> None:
        """Refuses a geography position past the poles or the antimeridian - a database may wrap it
        around the globe, storing another place.

        Args:
            geometry: The geometry.

        Raises:
            ValidationError: A longitude is past ±180 or a latitude past ±90 degrees.
        """
        # Local import: the extent field imports this module.
        from hare.gis.fields.extent_field import ExtentField

        for position in ExtentField.get_positions(geometry.get_coordinates()):
            longitude, latitude = position[0], position[1]
            if not -GEOGRAPHY_LONGITUDE_LIMIT <= longitude <= GEOGRAPHY_LONGITUDE_LIMIT or not (
                -GEOGRAPHY_LATITUDE_LIMIT <= latitude <= GEOGRAPHY_LATITUDE_LIMIT
            ):
                raise ValidationError(
                    f"{self.model_field_name}: the position ({longitude}, {latitude}) is off the globe - a "
                    f"longitude goes from -{GEOGRAPHY_LONGITUDE_LIMIT} to {GEOGRAPHY_LONGITUDE_LIMIT}, a latitude "
                    f"from -{GEOGRAPHY_LATITUDE_LIMIT} to {GEOGRAPHY_LATITUDE_LIMIT}"
                )

    def to_python(self, value: Any) -> Geometry | None:
        if value is None:
            return None
        return self.get_parsed_geometry(value)

    def to_db_value(self, value: Any, instance: type[Model] | Model | None) -> Any:
        self.validate(value)
        if value is None:
            return None
        return self.get_checked_geometry(self.get_parsed_geometry(value)).ewkt

    def raise_if_unsupported_by(self, connection: DatabaseClient) -> None:
        if self.geography:
            SpatialFeatures.raise_if_unknown_reference_system(connection, self.model_field_name, self.srid)

    def get_lookups(self) -> dict[str, FieldLookup]:
        # Local import: the spatial lookups import this module.
        from hare.gis.lookups.spatial_field_lookups import SpatialFieldLookups

        return SpatialFieldLookups.get_lookups(self)

    def get_lookup_value_description(self, lookup: str) -> tuple[LookupValueShape, Any] | None:
        """``dwithin`` and the distance lookups take ``(geometry, distance)``, ``relate`` ``(geometry,
        pattern)``, the other spatial relations (``contains`` among them) a geometry."""
        if lookup == SpatialRelation.DWITHIN or lookup in DISTANCE_LOOKUP_COMPARISONS:
            return LookupValueShape.VALUE, (Geometry, float)
        if lookup == SpatialRelation.RELATE:
            return LookupValueShape.VALUE, (Geometry, str)
        if lookup in SpatialRelation:
            return LookupValueShape.VALUE, Geometry
        return None

    def get_path_segments(self) -> tuple[str, ...]:
        """A point's coordinates (``x``, ``y``, ``z``) and the SRID (``srid``)."""
        return tuple(self.PATH_FUNCTION_TYPES)

    def get_path_transform(self, segment: str) -> tuple[Callable[[Term], Term], Field[Any]] | None:
        """A coordinate of a point (``location__x``, ``__y``, ``__z``) or the SRID (``__srid``)."""
        # Local import: the spatial terms import the SQL functions, which import the fields package.
        from hare.gis.terms.spatial_function_term import SpatialFunctionTerm

        function_type = self.PATH_FUNCTION_TYPES.get(segment)
        if function_type is None:
            return None
        is_srid = function_type == SpatialFunctionType.SRID
        output_field: Field[Any] = self.SRID_FIELD if is_srid else self.COORDINATE_FIELD
        return (
            partial(SpatialFunctionTerm, function_type, geography=self.geography, geometry_fields=(self,)),
            output_field,
        )
