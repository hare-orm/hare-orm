from __future__ import annotations

import datetime
from typing import TYPE_CHECKING, Any, TypeVar

from hare.dialects.enums import DialectName
from hare.exceptions import ValidationError
from hare.fields.data.temporal.temporal_values import TemporalValues
from hare.fields.field import Field

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model
    from hare.query.filters.lookups.field_lookup import FieldLookup

TTime = TypeVar("TTime", datetime.time, datetime.time | None)


class NaiveTimeField(Field[TTime]):
    """A ``time`` - a wall clock time of day without a time zone, held as a naive time whatever
    ``use_timezone`` says; a time with a time zone is refused. ``TimeField`` writes a ``timetz``."""

    SQL_TYPE = "TIME"
    SUPPORTED_DIALECTS = frozenset({DialectName.POSTGRESQL})
    field_type = datetime.time

    def get_lookups(self) -> dict[str, FieldLookup]:
        """The generic lookups and the parts of the time of day (``__hour``, ``__minute``, ...)."""
        # Local import: the filters package imports the fields package.
        from hare.query.filters.constants import TIME_OF_DAY_DATE_PART_LOOKUPS
        from hare.query.filters.lookups.field_lookups import FieldLookups

        return FieldLookups.get_date_parts(self, TIME_OF_DAY_DATE_PART_LOOKUPS)

    def to_python(self, value: Any) -> datetime.time | None:
        return None if value is None else self.get_naive_time(value)

    def to_db_value(self, value: Any, instance: type[Model] | Model) -> Any:
        return super().to_db_value(self.to_python(value), instance)

    def get_naive_time(self, value: Any) -> datetime.time:
        """Converts an accepted input into a naive time.

        Args:
            value: A naive time or an ISO 8601 string without an offset.

        Returns:
            The time.

        Raises:
            ValidationError: The value has another type, holds a time zone, or doesn't denote a time.
        """
        if isinstance(value, str):
            value = TemporalValues.parse_text(self, value, datetime.time.fromisoformat, catch=ValueError)
        if not isinstance(value, datetime.time):
            raise ValidationError(
                f"{self.model_field_name}: expected a time or an ISO 8601 string, got {type(value).__name__}"
            )
        if value.tzinfo is not None:
            raise ValidationError(
                f"{self.model_field_name}: {self.get_value_for_message(value)} holds a time zone - a time column "
                "keeps a wall clock time without one"
            )
        return value
