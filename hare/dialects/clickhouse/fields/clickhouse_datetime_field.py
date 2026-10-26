from __future__ import annotations

import datetime
import zoneinfo
from typing import TYPE_CHECKING, Any, TypeVar

from hare.classes.value_descriptions import ValueDescriptions
from hare.dialects.clickhouse.fields.constants import (
    CLICKHOUSE_DATE32_RANGE,
    CLICKHOUSE_DATETIME64_MAX_PRECISION,
    CLICKHOUSE_DATETIME64_NANOSECONDS_LAST_MOMENT,
    CLICKHOUSE_DATETIME64_RANGE,
    CLICKHOUSE_DATETIME_RANGE,
    PYTHON_DATETIME_PRECISION,
)
from hare.dialects.clickhouse.fields.narrowing.moment_limit import MomentLimit
from hare.exceptions import ConfigurationError, ValidationError
from hare.fields.data.temporal.date_field import DateField
from hare.fields.data.temporal.datetime_field import DatetimeField

if TYPE_CHECKING:
    from hare.fields.field import Field
    from hare.fields.narrowing.narrowing_limit import NarrowingLimit
    from hare.models import Model

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

    def get_moment_bounds(self) -> tuple[datetime.datetime, datetime.datetime, int]:
        """The first and the last moment the column holds, and its digits after the second.

        Returns:
            The bounds and the digits.
        """
        if self.precision is None:
            return (*CLICKHOUSE_DATETIME_RANGE, 0)
        first_moment, last_moment = CLICKHOUSE_DATETIME64_RANGE
        if self.precision == CLICKHOUSE_DATETIME64_MAX_PRECISION:
            last_moment = CLICKHOUSE_DATETIME64_NANOSECONDS_LAST_MOMENT
        return first_moment, last_moment, self.precision

    def get_narrowing_limit(self, old_field: Field[Any]) -> NarrowingLimit | None:
        first_moment, last_moment, digits = self.get_moment_bounds()
        if isinstance(old_field, ClickhouseDatetimeField):
            old_first_moment, old_last_moment, old_digits = old_field.get_moment_bounds()
        elif isinstance(old_field, DatetimeField):
            old_first_moment, old_last_moment, old_digits = (*CLICKHOUSE_DATETIME64_RANGE, PYTHON_DATETIME_PRECISION)
        elif isinstance(old_field, DateField):
            first_day, last_day = CLICKHOUSE_DATE32_RANGE
            old_first_moment = datetime.datetime.combine(first_day, datetime.time(), datetime.UTC)
            old_last_moment = datetime.datetime.combine(last_day, datetime.time(), datetime.UTC)
            old_digits = 0
        else:
            return None
        if first_moment <= old_first_moment and old_last_moment <= last_moment and old_digits <= digits:
            return None
        # A moment outside the column's would be written as the nearest of them, the digits past its
        # precision cut off.
        return MomentLimit(first_moment, last_moment, digits)

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
        first_moment, last_moment = CLICKHOUSE_DATETIME_RANGE
        if precision < PYTHON_DATETIME_PRECISION:
            quantum = 10 ** (PYTHON_DATETIME_PRECISION - precision)
            remainder = value.microsecond % quantum
            # The column counts ticks from 1970 (``first_moment``) - a moment before it is a negative
            # count, whose tie rounds away from zero to the earlier tick.
            is_before_epoch = (
                value if value.tzinfo is not None else value.replace(tzinfo=datetime.UTC)
            ) < first_moment
            value -= datetime.timedelta(microseconds=remainder)
            if remainder * 2 > quantum or (remainder * 2 == quantum and not is_before_epoch):
                value += datetime.timedelta(microseconds=quantum)
        column_first_moment, column_last_moment, _digits = field.get_moment_bounds()
        # A DateTime and a DateTime64(9) hold fewer moments than the 1900 to 2299 every moment is checked against.
        if (column_first_moment, column_last_moment) != CLICKHOUSE_DATETIME64_RANGE:
            moment = value if value.tzinfo is not None else value.replace(tzinfo=datetime.UTC)
            if not column_first_moment <= moment <= column_last_moment:
                type_name = "DateTime" if field.precision is None else f"DateTime64({field.precision})"
                raise ValidationError(
                    f"{field.model_field_name}: {value} is outside the moments a {type_name} column holds, "
                    f"{column_first_moment} to {column_last_moment}"
                )
        return ClickhouseTypedValue(value, field.SQL_TYPE)
