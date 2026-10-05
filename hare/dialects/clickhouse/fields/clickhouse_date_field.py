from __future__ import annotations

import datetime
from typing import TYPE_CHECKING, Any, TypeVar

from hare.dialects.clickhouse.enums import ClickhouseDialectName
from hare.dialects.clickhouse.fields.constants import CLICKHOUSE_DATE_RANGE
from hare.dialects.clickhouse.types.declarations import ClickhouseTypedValue
from hare.exceptions import ValidationError
from hare.fields.data.temporal.date_field import DateField

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model

TDate = TypeVar("TDate", datetime.date, datetime.date | None)


class ClickhouseDateField(DateField[TDate]):
    """``Date`` - a day from 1970-01-01 to 2149-06-06 in two bytes; a day outside them is refused, as
    the server would write the nearest one. ``DateField`` writes the wider ``Date32``."""

    SQL_TYPE = "Date"
    SUPPORTED_DIALECTS = frozenset({ClickhouseDialectName.CLICKHOUSE})

    def to_db_value(self, value: Any, instance: type[Model] | Model) -> Any:
        value = super().to_db_value(value, instance)
        first_day, last_day = CLICKHOUSE_DATE_RANGE
        if isinstance(value, datetime.date) and not first_day <= value <= last_day:
            raise ValidationError(
                f"{self.model_field_name}: {value} is outside the days a Date column holds, {first_day} to {last_day}"
            )
        # Typed as the column - a day's literal is a Date32, which VALUES doesn't write into a Date.
        return value if value is None else ClickhouseTypedValue(value, self.SQL_TYPE)
