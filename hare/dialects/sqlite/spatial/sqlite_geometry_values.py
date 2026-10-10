from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from hare.dialects.sqlite.spatial.constants import SPATIALITE_COLUMN_TYPES
from hare.dialects.sqlite.spatial.spatialite_blob_reader import SpatialiteBlobReader
from hare.dialects.sqlite.spatial.spatialite_blob_writer import SpatialiteBlobWriter

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.fields.field import Field
    from hare.gis.fields.geometry_field import GeometryField
    from hare.models import Model


class SqliteGeometryValues:
    """How SQLite stores a ``GeometryField`` - SpatiaLite's own BLOB, written and read by hare itself,
    so a geometry is stored and compared without the SpatiaLite extension; only the spatial lookups,
    functions and aggregates need it."""

    @staticmethod
    def get_column_type(field: Field[Any]) -> str:
        """The column type - the geometry type's name, with ``Z`` for x/y/z (``POINTZ``).

        Args:
            field: The geometry field.

        Returns:
            The column type.
        """
        geometry_field = cast("GeometryField", field)
        column_type = SPATIALITE_COLUMN_TYPES[geometry_field.get_geometry_type()]
        return f"{column_type}Z" if geometry_field.dimensions == 3 else column_type

    @staticmethod
    def to_db(field: Field[Any], value: Any, instance: type[Model] | Model) -> bytes | None:
        """The BLOB of a written geometry - of the field's type, dimensions and SRID.

        Raises:
            ValidationError: The value isn't such a geometry, or is empty.
        """
        geometry_field = cast("GeometryField", field)
        geometry_field.validate(value)
        if value is None:
            return None
        geometry = geometry_field.get_checked_geometry(geometry_field.get_parsed_geometry(value))
        return SpatialiteBlobWriter.write(geometry)

    @staticmethod
    def to_lookup(field: Field[Any], value: Any, instance: type[Model] | Model) -> bytes | None:
        """The BLOB an equality compares the column with.

        Raises:
            ValidationError: The value isn't a geometry, or is empty.
        """
        if value is None:
            return None
        return SpatialiteBlobWriter.write(cast("GeometryField", field).get_parsed_geometry(value))

    @staticmethod
    def to_python(field: Field[Any], value: Any) -> Any:
        """The geometry of a read BLOB."""
        if isinstance(value, (bytes, bytearray, memoryview)):
            return SpatialiteBlobReader.read(value)
        return field.to_python(value)

    @staticmethod
    def to_python_extent(field: Field[Any], value: Any) -> Any:
        """The bounding box of ``Extent`` - SpatiaLite gives the box's polygon."""
        if isinstance(value, (bytes, bytearray, memoryview)):
            value = SpatialiteBlobReader.read(value)
        return field.to_python(value)
