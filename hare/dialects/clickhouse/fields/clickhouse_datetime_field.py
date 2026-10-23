from __future__ import annotations

import datetime
import zoneinfo
from typing import TYPE_CHECKING, Any, TypeVar

from hare.classes.value_descriptions import ValueDescriptions
from hare.dialects.clickhouse.fields.constants import (
    CLICKHOUSE_DATETIME64_MAX_PRECISION,
    CLICKHOUSE_DATETIME_RANGE,
    PYTHON_DATETIME_PRECISION,
)
from hare.exceptions import ConfigurationError, ValidationError
from hare.fields.data.temporal.datetime_field import DatetimeField
from hare.query.enums import Lookup

if TYPE_CHECKING:
    from hare.dialects.base.dialect import Dialect
    from hare.fields.field import Field
    from hare.models import Model
    from hare.query.filters.lookups.field_lookup import FieldLookup

TDatetime = TypeVar("TDatetime", datetime.datetime, datetime.datetime | None)


class ClickhouseDatetimeField(DatetimeField[TDatetime]):
    """A moment as ClickHouse's own types keep it: ``DateTime`` - whole seconds from 1970 to 2106 -
    without ``precision``, ``DateTime64(precision)`` - digits after the second, from 1900 to 2299 -
    with it; in the column's ``timezone``. A written moment is rounded to the column's precision, as a
    ``DecimalField`` rounds to its places. ``DatetimeField`` writes ``DateTime64(6, 'UTC')``.

    Args:
        auto_now: Set the current time on every save.
        auto_now_add: Set the current time on the first save.
        precision: The digits after the second, 0 to 9 - None for ``DateTime``.
        timezone: The column's time zone - an IANA name; None for the server's own.

    Raises:
        ConfigurationError: ``precision`` isn't an int from 0 to 9, or ``timezone`` names no zone.
    """

    COLUMN_TYPE_FROM_DIALECT = True

    def __init__(
        self,
        auto_now: bool = False,
        auto_now_add: bool = False,
        *,
        precision: int | None = None,
        timezone: str | None = "UTC",
        **kwargs: Any,
    ) -> None:
        if precision is not None and (
            type(precision) is not int or not 0 <= precision <= CLICKHOUSE_DATETIME64_MAX_PRECISION
        ):
            raise ConfigurationError(
                f"ClickhouseDatetimeField(precision=...) takes None or an int from 0 to "
                f"{CLICKHOUSE_DATETIME64_MAX_PRECISION}, got {ValueDescriptions.get_shown_value(precision)}"
            )
        if timezone is not None:
            try:
                zoneinfo.ZoneInfo(timezone)
            except (TypeError, ValueError, zoneinfo.ZoneInfoNotFoundError):
                raise ConfigurationError(
                    f"ClickhouseDatetimeField(timezone=...) takes an IANA time zone name or None, got "
                    f"{ValueDescriptions.get_shown_value(timezone)}"
                ) from None
        self.precision = precision
        self.timezone = timezone
        super().__init__(auto_now, auto_now_add, **kwargs)  # type: ignore[misc]

    @property
    def SQL_TYPE(self) -> str:  # type: ignore[override]
        timezone_argument = "" if self.timezone is None else f"'{self.timezone}'"
        if self.precision is None:
            return f"DateTime({timezone_argument})" if timezone_argument else "DateTime"
        return f"DateTime64({self.precision}{', ' + timezone_argument if timezone_argument else ''})"

    def get_lookups(self) -> dict[str, FieldLookup]:
        lookups = super().get_lookups()
        if self.precision is not None:
            return lookups
        # The list of IN is typed as the DateTime column - ClickHouse compares no DateTime64 there.
        return {
            **lookups,
            Lookup.IN: lookups[Lookup.IN].with_changes(value_encoder=self.encode_whole_second_list),
            Lookup.NOT_IN: lookups[Lookup.NOT_IN].with_changes(value_encoder=self.encode_whole_second_list),
        }

    def encode_whole_second_list(
        self, values: Any, obj: Model | type[Model], field: Field[Any], dialect: Dialect
    ) -> list[Any]:
        """The values of ``__in``/``__not_in`` of a ``DateTime`` column, typed as the column.

        Args:
            values: The values.
            obj: The model or object.
            field: The field.
            dialect: The dialect.

        Returns:
            The encoded values.

        Raises:
            ValidationError: A moment has a fraction of a second - no value of the column.
        """
        # Local imports: the dialect's types and the filters import the fields.
        from hare.dialects.clickhouse.types.declarations import ClickhouseTypedValue
        from hare.query.filters.lookups.value_encoders import ValueEncoders

        encoded_values = ValueEncoders.encode_list(values, obj, field, dialect)
        typed_values = []
        for value in encoded_values:
            if isinstance(value, datetime.datetime):
                if value.microsecond:
                    raise ValidationError(
                        f"{self.model_field_name}: {self.get_value_for_message(value)} has a fraction of a second - "
                        "a DateTime column holds whole seconds; compare it with __gt/__lt or __range"
                    )
                value = ClickhouseTypedValue(value, self.SQL_TYPE)
            typed_values.append(value)
        return typed_values

    @staticmethod
    def to_db_rounded_value(field: Any, value: Any, instance: type[Model] | Model | None) -> Any:
        """The moment written to the column - its instant rounded to the column's precision, a tie away
        from zero; a moment outside a ``DateTime`` column's range refused.

        Args:
            field: The field.
            value: The value.
            instance: The model (class) it is written for.

        Returns:
            The value to bind, typed as the column - a moment's literal is a ``DateTime64(6)``, which
            ``VALUES`` doesn't write into another type.

        Raises:
            ValidationError: The moment is outside the column's range.
        """
        # Local import: the dialect's types import its fields.
        from hare.dialects.clickhouse.types.declarations import ClickhouseTypedValue

        value = DatetimeField.to_db_instant_value(field, value, instance)
        if not isinstance(value, datetime.datetime):
            return value
        precision = 0 if field.precision is None else field.precision
        if precision < PYTHON_DATETIME_PRECISION:
            quantum = 10 ** (PYTHON_DATETIME_PRECISION - precision)
            remainder = value.microsecond % quantum
            value -= datetime.timedelta(microseconds=remainder)
            if remainder * 2 >= quantum:
                value += datetime.timedelta(microseconds=quantum)
        if field.precision is None:
            moment = value if value.tzinfo is not None else value.replace(tzinfo=datetime.UTC)
            first_moment, last_moment = CLICKHOUSE_DATETIME_RANGE
            if not first_moment <= moment <= last_moment:
                raise ValidationError(
                    f"{field.model_field_name}: {value} is outside the moments a DateTime column holds, "
                    f"{first_moment} to {last_moment}"
                )
        return ClickhouseTypedValue(value, field.SQL_TYPE)
