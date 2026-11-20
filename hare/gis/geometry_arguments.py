from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.gis.fields.geometry_field import GeometryField
from hare.gis.geometries.geometry import Geometry
from hare.gis.terms.geometry_value import GeometryValue

if TYPE_CHECKING:
    from hare.dialects.base.dialect import Dialect


class GeometryArguments:
    """Prepare Python geometry arguments consistently for functions and lookups."""

    @staticmethod
    def get_geometry(value: Any, field: GeometryField | None) -> Geometry:
        """Parse, validate and describe conversion to the target SRID.

        Spatial comparisons may use another geometry type or SRID. The database performs the
        declared transform; geographic bounds are checked on coordinates already in that SRID.
        Field validators receive the parsed geometry rather than one of its interchange forms.
        """
        source = field if field is not None else GeometryField()
        geometry = source.get_parsed_geometry(value)
        source.validate(geometry)
        if source.geography and geometry.srid == source.srid and not geometry.is_empty:
            source.raise_if_off_the_globe(geometry)
        return geometry

    @staticmethod
    def get_value(value: Any, field: GeometryField | None, *, dialect: Dialect) -> GeometryValue:
        """Create the SQL geometry argument with an explicit target reference system."""
        geometry = GeometryArguments.get_geometry(value, field)
        term = GeometryValue(
            geometry,
            field.srid if field is not None else geometry.srid,
            field.geography if field is not None else False,
            dialect=dialect,
        )
        if field is not None:
            term.args[0].sensitive = field.sensitive
        return term

    @staticmethod
    def encode_parameter(geometry: Geometry, dialect: Dialect) -> Any:
        """Encode the literal's actual domain once through the dialect's canonical writer.

        A spatial operand need not have its column's shape or reference system. Literal
        descriptors already describe that domain without applying the column's constraints.
        """
        from hare.query.expressions.value import Value

        field: GeometryField = Value.get_literal_output_field(geometry)  # type: ignore[assignment]
        return dialect.types.get_db_writer(field)(geometry, None)

    @staticmethod
    def get_parameter_value(value: Any, *, field: GeometryField | None, dialect: Dialect) -> Any:
        """Convert a fresh parameter using the same preparation as its recorded SQL wrapper."""
        return GeometryArguments.encode_parameter(GeometryArguments.get_geometry(value, field), dialect)
