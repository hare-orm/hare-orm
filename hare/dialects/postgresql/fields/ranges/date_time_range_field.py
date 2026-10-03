from __future__ import annotations

import datetime
from typing import Any, ClassVar

from hare.dialects.postgresql.fields.ranges.range import Range
from hare.dialects.postgresql.fields.ranges.range_field import RangeField
from hare.exceptions import ValidationError
from hare.fields import Field
from hare.fields.constants import NAIVE_INFINITY_DATETIMES
from hare.fields.data.temporal.datetime_field import DatetimeField
from hare.fields.data.temporal.parse_datetime import parse_datetime
from hare.query.rows.enums import RangeBoundKind
from hare.utils import Timezone


class DateTimeRangeField(RangeField):
    """``tstzrange`` - a range of timezone-aware timestamps. With an exclusion constraint on it, "no
    two rows of one resource overlap in time" is a database guarantee; on a model with
    ``Meta.tenant_field`` the constraint must name the tenant column itself.

    Bounds follow ``DatetimeField``: a naive bound is a wall clock in the configured zone under
    ``use_tz=True`` (the system's local zone otherwise), and bounds are read back in the configured
    zone. ``infinity``/``-infinity`` read as ``datetime.max``/``datetime.min`` and are written back
    as that infinity.
    """

    SQL_TYPE = "tstzrange"
    ELEMENT_SQL_TYPE = "timestamptz"
    NATIVE_BOUND_KIND = RangeBoundKind.DATETIME

    #: Shared field a bound is read through - the statement plans hold output fields weakly.
    BOUND_FIELD: ClassVar[DatetimeField[Any]] = DatetimeField()

    def get_bound_field(self) -> Field[Any]:
        return self.BOUND_FIELD  # type: ignore[call-overload]

    def parse_bound_text(self, text: str) -> Any:
        # `infinity`/`-infinity` read as datetime.max/datetime.min, as get_python_bound() expects.
        if text == "infinity":
            return datetime.datetime.max.replace(tzinfo=datetime.UTC)
        if text == "-infinity":
            return datetime.datetime.min.replace(tzinfo=datetime.UTC)
        return parse_datetime(text)

    @staticmethod
    def get_infinite_bound(value: Any) -> Any:
        """The UTC form of a naive ``datetime.max``/``datetime.min`` bound, the Python stand-ins
        for ``infinity``/``-infinity``.

        Args:
            value: A bound.

        Returns:
            The bound in UTC when it is a naive infinity stand-in, else ``value`` unchanged.
        """
        if (
            isinstance(value, datetime.datetime)
            and value.tzinfo is None
            and value in (datetime.datetime.max, datetime.datetime.min)
        ):
            return value.replace(tzinfo=datetime.UTC)
        return value

    def get_python_bound(self, value: Any) -> Any:
        """One bound as a ``DatetimeField`` reads a value: in the configured zone under
        ``use_tz=True``, the system's naive local time otherwise.

        Args:
            value: A bound, None for unbounded.

        Returns:
            The converted bound; an infinity stand-in stays ``datetime.max``/``datetime.min``
            (aware UTC under ``use_tz=True``, naive otherwise).

        Raises:
            ValidationError: The bound falls outside the datetime range once converted.
        """
        if not isinstance(value, datetime.datetime):
            return value
        use_tz = Timezone.get_use_tz()
        if value.replace(tzinfo=None) in NAIVE_INFINITY_DATETIMES and (
            value.tzinfo is None or value.utcoffset() == datetime.timedelta(0)
        ):
            return value.replace(tzinfo=datetime.UTC) if use_tz else value.replace(tzinfo=None)
        try:
            if use_tz:
                if Timezone.is_naive(value):
                    return Timezone.make_aware(value, Timezone.default())
                return value.astimezone(Timezone.default())
            if Timezone.is_aware(value):
                return Timezone.get_system_local_naive(value)
        except OverflowError:
            raise ValidationError(
                f"{self.model_field_name}: range bound {self.get_value_for_message(value)} is out of the "
                "supported datetime range once converted"
            ) from None
        return value

    def get_python_range(self, range_value: Range[Any] | None) -> Range[Any] | None:
        """A range with both bounds converted by ``get_python_bound``.

        Args:
            range_value: The range, or None.

        Returns:
            The converted range.
        """
        if range_value is None or range_value.is_empty:
            return range_value
        lower = self.get_python_bound(range_value.lower)
        upper = self.get_python_bound(range_value.upper)
        if lower is range_value.lower and upper is range_value.upper:
            return range_value
        return Range(lower=lower, upper=upper, lower_inc=range_value.lower_inc, upper_inc=range_value.upper_inc)

    def to_python(self, value: Any) -> Range[Any] | None:
        return self.get_python_range(super().to_python(value))

    def coerce_bound(self, value: Any) -> Any:
        if value is None:
            return value
        if isinstance(value, str):
            validation_error = None
            try:
                value = parse_datetime(value)
            except (ValueError, TypeError) as exc:
                validation_error = self.get_validation_error(exc, value)
            if validation_error is not None:
                raise validation_error
        if not isinstance(value, datetime.datetime):
            raise ValidationError(
                f"{self.model_field_name}: expected a datetime, got {self.get_value_for_message(value)}"
            )
        value = self.get_infinite_bound(value)
        try:
            if Timezone.is_naive(value):
                # A naive bound means what a naive DatetimeField value means: a wall clock in the
                # configured zone under use_tz=True, in the system's local zone otherwise.
                if Timezone.get_use_tz():
                    value = Timezone.make_aware(value, Timezone.default())
                else:
                    value = Timezone.make_system_local_aware(value)
            value.astimezone(datetime.UTC)
        except OverflowError:
            raise ValidationError(
                f"{self.model_field_name}: range bound {self.get_value_for_message(value)} is out of the "
                "supported datetime range once converted to UTC"
            ) from None
        return value
