from __future__ import annotations

import datetime
from typing import TYPE_CHECKING, Any, TypeVar

from hare.dialects.enums import DialectName
from hare.exceptions import ValidationError
from hare.fields.data.temporal.parse_datetime import parse_datetime
from hare.fields.data.temporal.temporal_values import TemporalValues
from hare.fields.field import Field

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model
    from hare.query.filters.lookups.field_lookup import FieldLookup

TDatetime = TypeVar("TDatetime", datetime.datetime, datetime.datetime | None)


class NaiveDatetimeField(Field[TDatetime]):
    """A ``timestamp`` - a date and a wall clock time without a time zone, held as a naive datetime
    whatever ``use_timezone`` says. An aware datetime names a moment, not a wall clock, and is refused.
    ``DatetimeField`` writes a ``timestamptz``.
    """

    SQL_TYPE = "TIMESTAMP"
    SUPPORTED_DIALECTS = frozenset({DialectName.POSTGRESQL})
    field_type = datetime.datetime

    def get_lookups(self) -> dict[str, FieldLookup]:
        """The generic lookups and the date parts of the value (``__year``, ``__hour``, ...)."""
        # Local import: the filters package imports the fields package.
        from hare.query.filters.constants import DATETIME_DATE_PART_LOOKUPS
        from hare.query.filters.lookups.field_lookups import FieldLookups

        return FieldLookups.get_date_parts(self, DATETIME_DATE_PART_LOOKUPS)

    def to_python(self, value: Any) -> datetime.datetime | None:
        return None if value is None else self.get_naive_datetime(value)

    def to_db_value(self, value: Any, instance: type[Model] | Model) -> Any:
        return super().to_db_value(self.to_python(value), instance)

    def get_naive_datetime(self, value: Any) -> datetime.datetime:
        """Converts an accepted input into a naive datetime.

        Args:
            value: A naive datetime, a date (its midnight) or an ISO 8601 string without an offset.

        Returns:
            The datetime.

        Raises:
            ValidationError: The value has another type, holds a time zone, or doesn't denote a
                datetime.
        """
        if isinstance(value, str):
            value = TemporalValues.parse_text(self, value, parse_datetime)
        elif isinstance(value, datetime.date) and not isinstance(value, datetime.datetime):
            value = datetime.datetime.combine(value, datetime.time.min)
        if not isinstance(value, datetime.datetime):
            raise ValidationError(
                f"{self.model_field_name}: expected a datetime, a date or an ISO 8601 string, "
                f"got {type(value).__name__}"
            )
        if value.tzinfo is not None:
            raise ValidationError(
                f"{self.model_field_name}: {self.get_value_for_message(value)} holds a time zone - a timestamp "
                "column keeps a wall clock time without one"
            )
        return value
