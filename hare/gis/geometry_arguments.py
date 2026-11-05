from __future__ import annotations

from typing import Any, cast

from hare.gis.fields.geometry_field import GeometryField
from hare.gis.geometries.geometry import Geometry
from hare.gis.terms.geometry_value import GeometryValue


class GeometryArguments:
    """Prepare Python geometry arguments consistently for functions and lookups."""

    @staticmethod
    def get_geometry(value: Any, field: GeometryField | None = None) -> Geometry:
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
    def get_ewkt(value: Any, *, field: GeometryField | None = None) -> str:
        """Encode a current input without constructing SQL nodes."""
        return GeometryArguments.get_geometry(value, field).ewkt

    @staticmethod
    def get_value(value: Any, field: GeometryField | None = None) -> GeometryValue:
        """Create the SQL geometry argument with an explicit target reference system."""
        geometry = GeometryArguments.get_geometry(value, field)
        term = GeometryValue(
            geometry,
            field.srid if field is not None else cast("int", geometry.srid),
            field.geography if field is not None else False,
        )
        if field is not None:
            term.args[0].sensitive = field.sensitive
        return term
