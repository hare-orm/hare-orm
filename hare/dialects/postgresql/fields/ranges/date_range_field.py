from __future__ import annotations

import datetime
from typing import Any, ClassVar

from hare.dialects.postgresql.fields.ranges.range_field import RangeField
from hare.fields import Field
from hare.fields.data.temporal.date_field import DateField
from hare.fields.data.temporal.parse_datetime import parse_datetime
from hare.query.rows.enums import RangeBoundKind


class DateRangeField(RangeField):
    """``daterange`` - a range of dates."""

    SQL_TYPE = "daterange"
    ELEMENT_SQL_TYPE = "date"

    #: Shared field a bound is read through - the statement plans hold output fields weakly.
    BOUND_FIELD: ClassVar[DateField[Any]] = DateField()

    def get_bound_field(self) -> Field[Any]:
        return self.BOUND_FIELD  # type: ignore[call-overload]

    DISCRETE_STEP = datetime.timedelta(days=1)
    NATIVE_BOUND_KIND = RangeBoundKind.DATE

    def parse_bound_text(self, text: str) -> Any:
        # `infinity`/`-infinity` read as date.max/date.min, the way the drivers decode them.
        if text == "infinity":
            return datetime.date.max
        if text == "-infinity":
            return datetime.date.min
        return datetime.date.fromisoformat(text)

    def coerce_bound(self, value: Any) -> Any:
        if value is None or isinstance(value, datetime.date):
            # datetime.datetime is itself a datetime.date subclass - narrowed to a plain date the
            # same way DateField.to_db_value() already does for an identical direct-assignment
            # trap, instead of silently letting a full datetime (with a time component) through.
            if isinstance(value, datetime.datetime):
                return value.date()
            return value
        try:
            return parse_datetime(value).date()
        except (ValueError, TypeError) as exc:
            validation_error = self.get_validation_error(exc, value)
        raise validation_error
