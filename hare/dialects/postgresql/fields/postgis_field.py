from __future__ import annotations

import dataclasses
import math
import struct
from decimal import Decimal
from typing import TYPE_CHECKING, Any, ClassVar

from hare.exceptions import ValidationError
from hare.fields import Field
from hare.fields.registrations.registered_lookup import RegisteredLookup
from hare.gis.constants import GEOGRAPHY_LATITUDE_LIMIT, GEOGRAPHY_LONGITUDE_LIMIT
from hare.query.filters.lookups.field_lookup import FieldLookup
from hare.query.filters.lookups.lookups import Lookups
from hare.sql.terms.functions.function import Function
from hare.sql.terms.term import Term

if TYPE_CHECKING:
    from hare.dialects.base.dialect import Dialect
    from hare.models import Model


class PostGISField(Field[tuple[float, float]]):
    """PostGIS ``geography(Point,4326)`` - a ``(latitude, longitude)`` tuple in degrees. Needs the
    PostGIS extension. For distances in meters use ``STDistance``/``STDWithin``, which can use a
    GiST index on the field.
    """

    COLUMN_TYPE_FROM_DIALECT = True

    SQL_TYPE = "geography(Point,4326)"
    field_type = tuple

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

    def get_lookups(self) -> dict[str, FieldLookup]:
        lookups = dict(super().get_lookups())
        # A geography has an = operator and no <> - the database would compare its text.
        lookups["not"] = dataclasses.replace(lookups["not"], operator=Lookups.not_equal_by_equality)
        lookups["not_in"] = dataclasses.replace(lookups["not_in"], operator=Lookups.not_in_by_equality)
        return lookups

    def get_python_type(self) -> Any:
        # `tuple[float, float]`, not the bare `tuple` of field_type - what a pydantic model
        # generated from the field accepts.
        return tuple[float, float]

    def to_db_value(self, value: tuple[float, float] | None, instance: type[Model] | Model | None) -> str | None:
        self.validate(value)
        if value is None:
            return None
        try:
            latitude, longitude = value
        except (ValueError, TypeError) as error:
            # A value of another shape raises ValidationError, not a ValueError/TypeError.
            raise ValidationError(f"{self.model_field_name}: {error}")
        self.raise_if_off_the_globe(latitude, longitude)
        # WKT string; PostGIS has an implicit text->geography cast, so no explicit
        # ST_GeogFromText is needed on INSERT/UPDATE.
        return f"POINT({longitude} {latitude})"

    def raise_if_off_the_globe(self, latitude: Any, longitude: Any) -> None:
        """Refuses a coordinate that is no finite number or lies past the poles or the antimeridian -
        PostGIS would wrap it around the globe, storing another place.

        Args:
            latitude: The latitude, in degrees.
            longitude: The longitude, in degrees.

        Raises:
            ValidationError: A coordinate is no number (a bool, a text), not finite, or out of range.
        """
        for name, coordinate, limit in (
            ("latitude", latitude, GEOGRAPHY_LATITUDE_LIMIT),
            ("longitude", longitude, GEOGRAPHY_LONGITUDE_LIMIT),
        ):
            if isinstance(coordinate, bool) or not isinstance(coordinate, (int, float, Decimal)):
                raise ValidationError(
                    f"{self.model_field_name}: the {name} takes a number, got {type(coordinate).__name__}"
                )
            if not math.isfinite(coordinate) or not -limit <= coordinate <= limit:
                raise ValidationError(
                    f"{self.model_field_name}: the {name} {coordinate} is off the globe - it goes from "
                    f"-{limit} to {limit}"
                )

    @staticmethod
    def raise_if_radius_unusable(radius: Any, name: str) -> None:
        """Refuses a radius that is no finite number of zero or more.

        Args:
            radius: The radius.
            name: What the radius is called in the message.

        Raises:
            ValidationError: The radius is no number (a bool, a text), not finite, or negative.
        """
        if isinstance(radius, bool) or not isinstance(radius, (int, float, Decimal)):
            raise ValidationError(f"{name} takes a number, got {type(radius).__name__}")
        if not math.isfinite(radius) or radius < 0:
            raise ValidationError(f"{name} takes a finite number of zero or more, got {radius}")

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
    def get_validated_point_text(
        target_field: Field[Any] | None, instance: type[Model] | Model, point: tuple[float, float]
    ) -> str:
        """The well-known text of a ``(latitude, longitude)`` pair, validated through ``target_field`` -
        the encoder of a bound point, run by a reused plan too.

        Args:
            target_field: The field the point is compared with.
            instance: The model or object.
            point: The pair.

        Returns:
            The text.
        """
        PostGISField.validate_point_against_field(point, target_field, instance)
        return PostGISField.get_point_text(point)

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
            PostGISField.raise_if_radius_unusable(radius_km, f"{field_object.model_field_name}__within_km's radius")
            return value

        def _operator(term: Term, value: tuple[float, float, float]) -> Term:
            latitude, longitude, radius_km = value
            point = PostGISField.get_geography_term((latitude, longitude))
            return Function("ST_DWITHIN", term, point, radius_km * 1000)

        return FieldLookup(_operator, _encode_within_km_value)

    #: The field's own lookup, declared with the class - no query is built before it exists.
    registered_lookups: ClassVar[dict[str, RegisteredLookup]] = {
        "within_km": RegisteredLookup(builder=_within_km_lookup, value_type=tuple)
    }
