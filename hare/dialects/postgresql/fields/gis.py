from __future__ import annotations

import struct
from typing import TYPE_CHECKING, Any

from hare.dialects.enums import DialectName
from hare.exceptions import ValidationError
from hare.fields import Field
from hare.query.filters.field_lookup import FieldLookup
from hare.sql.terms.base.term import Term
from hare.sql.terms.functions.function import Function

if TYPE_CHECKING:
    from hare.dialects.base.dialect import Dialect  # pragma: nocoverage
    from hare.models import Model


class PostGISField(Field[tuple[float, float]]):
    """PostGIS ``geography(Point,4326)`` - a ``(latitude, longitude)`` tuple in degrees. Needs the
    PostGIS extension. For distances in meters use ``STDistance``/``STDWithin``, which can use a
    GiST index on the field.
    """

    SUPPORTED_DIALECTS = frozenset({DialectName.POSTGRESQL})

    SQL_TYPE = "geography(Point,4326)"
    field_type = tuple
    requires_extension = "postgis"

    @staticmethod
    def get_geography_term(point: Term | tuple[float, float]) -> Term:
        """A geography value: ``ST_GeogFromText('POINT(lon lat)')`` for a ``(latitude, longitude)``
        pair, a term (another geography column) as it is."""
        if isinstance(point, tuple):
            return Function("ST_GEOGFROMTEXT", PostGISField.get_point_text(point))
        return point

    @staticmethod
    def get_point_text(point: tuple[float, float]) -> str:
        """The well-known text of a ``(latitude, longitude)`` pair - ``POINT(lon lat)``.

        Args:
            point: The pair.

        Returns:
            The text.
        """
        latitude, longitude = point
        return f"POINT({longitude} {latitude})"

    def get_python_type(self) -> Any:
        # `tuple[float, float]`, not the bare `tuple` of field_type - what a pydantic model
        # generated from the field accepts.
        return tuple[float, float]

    def to_db_value(self, value: tuple[float, float] | None, instance: type[Model] | Model) -> str | None:
        self.validate(value)
        if value is None:
            return None
        try:
            latitude, longitude = value
        except (ValueError, TypeError) as exc:
            # A value of another shape raises ValidationError, not a ValueError/TypeError.
            raise ValidationError(f"{self.model_field_name}: {exc}")
        # WKT string; PostGIS has an implicit text->geography cast, so no explicit
        # ST_GeogFromText is needed on INSERT/UPDATE.
        return f"POINT({longitude} {latitude})"

    def to_python(self, value: Any) -> tuple[float, float] | None:
        if value is None or isinstance(value, tuple):
            return value
        if isinstance(value, list):
            # A point assigned as a list is held as the tuple a read gives.
            return tuple(value)
        if not isinstance(value, str):
            raise ValidationError(
                f"{self.model_field_name}: expected a (latitude, longitude) tuple, "
                f"got {self.get_value_for_message(value)}"
            )
        try:
            return self._parse_ewkb_point(value)
        except (ValueError, IndexError, struct.error) as error:
            raise self.get_validation_error(error, value) from None

    @staticmethod
    def validate_point_against_field(
        point: Any, target_field: Field[Any] | None, instance: type[Model] | Model
    ) -> None:
        """Validates a raw ``(latitude, longitude)`` point through ``target_field``, its validators
        included. A no-op when ``point`` isn't a raw tuple or ``target_field`` isn't a
        ``PostGISField``.
        """
        if isinstance(point, tuple) and isinstance(target_field, PostGISField):
            target_field.to_db_value(point, instance)

    @staticmethod
    def _parse_ewkb_point(value: str) -> tuple[float, float]:
        """Parses the EWKB hex of a POINT with an SRID - byte order (1), type with the SRID flag (4),
        SRID (4), X (8), Y (8).

        Returns:
            ``(latitude, longitude)``.
        """
        raw = bytes.fromhex(value)
        byte_order = "<" if raw[0] == 1 else ">"
        type_with_srid_flag = struct.unpack(byte_order + "I", raw[1:5])[0]
        offset = 9 if type_with_srid_flag & 0x20000000 else 5  # +4 bytes if SRID flag is set
        longitude, latitude = struct.unpack(byte_order + "dd", raw[offset : offset + 16])
        return latitude, longitude

    @staticmethod
    def _within_km_lookup(field: Field[Any] | None) -> FieldLookup:
        """``.filter(location__within_km=(lat, lon, radius_km))``. The lookup's encoder validates the
        ``(lat, lon)`` pair through the field, its validators included.
        """

        def _encode_within_km_value(
            value: tuple[float, float, float], model: type[Model] | Model, field_object: Field[Any], dialect: Dialect
        ) -> tuple[float, float, float]:
            latitude, longitude, radius_km = value
            field_object.to_db_value((latitude, longitude), model)
            return value

        def _operator(term: Term, value: tuple[float, float, float]) -> Term:
            latitude, longitude, radius_km = value
            point = PostGISField.get_geography_term((latitude, longitude))
            return Function("ST_DWITHIN", term, point, radius_km * 1000)

        return FieldLookup(_operator, _encode_within_km_value)


PostGISField.register_lookup("within_km", PostGISField._within_km_lookup, value_type=tuple)
