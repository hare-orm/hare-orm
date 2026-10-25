from __future__ import annotations

import datetime
from typing import TYPE_CHECKING, Any, TypeVar

from hare.dialects.clickhouse.fields.constants import CLICKHOUSE_DATE_RANGE
from hare.dialects.clickhouse.types.declarations import ClickhouseTypedValue
from hare.exceptions import ValidationError
from hare.fields.data.temporal.date_field import DateField
from hare.fields.data.temporal.datetime_field import DatetimeField
from hare.fields.narrowing.day_limit import DayLimit

if TYPE_CHECKING:
    from hare.fields.field import Field
    from hare.fields.narrowing.narrowing_limit import NarrowingLimit
    from hare.models import Model

TDate = TypeVar("TDate", datetime.date, datetime.date | None)


class ClickhouseDateField(DateField[TDate]):
    """``Date`` - a day from 1970-01-01 to 2149-06-06 in two bytes; a day outside them is refused, as
    the server would write the nearest one. ``DateField`` writes the wider ``Date32``."""

    SQL_TYPE = "Date"
    COLUMN_TYPE_FROM_DIALECT = True

    def get_narrowing_limit(self, old_field: Field[Any]) -> NarrowingLimit | None:
        if isinstance(old_field, ClickhouseDateField):
            return None
        first_day, last_day = CLICKHOUSE_DATE_RANGE
        # A day outside the column's would be written as the nearest of them, a moment's time of day
        # cut off.
        if isinstance(old_field, DatetimeField):
            return DayLimit(first_day, last_day, checks_time_of_day=True)
        if isinstance(old_field, DateField):
            return DayLimit(first_day, last_day, checks_time_of_day=False)
        return None

    def to_db_value(self, value: Any, instance: type[Model] | Model | None) -> Any:
        value = super().to_db_value(value, instance)
        first_day, last_day = CLICKHOUSE_DATE_RANGE
        if isinstance(value, datetime.date) and not first_day <= value <= last_day:
            raise ValidationError(
                f"{self.model_field_name}: {value} is outside the days a Date column holds, {first_day} to {last_day}"
            )
        # Typed as the column - a day's literal is a Date32, which VALUES doesn't write into a Date.
        return value if value is None else ClickhouseTypedValue(value, self.SQL_TYPE)
