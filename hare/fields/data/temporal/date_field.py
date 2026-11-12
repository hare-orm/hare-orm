from __future__ import annotations

import datetime
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal, TypeVar, overload

from hare.exceptions import ValidationError
from hare.fields.data.temporal.parse_datetime import parse_datetime
from hare.fields.db_defaults.now import Now
from hare.fields.enums import NowValueType
from hare.fields.field import Field
from hare.fields.narrowing.day_limit import DayLimit
from hare.sql.enums import DateTruncSource

if TYPE_CHECKING:
    from hare.fields.narrowing.narrowing_limit import NarrowingLimit
    from hare.models import Model
    from hare.query.filters.lookups.field_lookup import FieldLookup
from hare.fields.data.temporal.temporal_values import TemporalValues

TDate = TypeVar("TDate", datetime.date, datetime.date | None)


class DateField(Field[TDate]):
    """A calendar date - a ``DATE`` column. Takes a ``date``, or ISO text of a full date."""

    field_type = datetime.date
    accepted_value_types = (str,)
    date_function_source = DateTruncSource.DATE

    keeps_native_db_values = True
    SQL_TYPE = "DATE"

    @overload
    def __init__(self: DateField[datetime.date], *, null: Literal[False] = False, **kwargs: Any) -> None: ...

    @overload
    def __init__(self: DateField[datetime.date | None], *, null: Literal[True], **kwargs: Any) -> None: ...

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        if isinstance(self.db_default, Now):
            self.db_default = self.db_default.get_for_value_type(NowValueType.DATE)

    def get_narrowing_limit(self, old_field: Field[Any]) -> NarrowingLimit | None:
        # Imported here: the modules import each other.
        from hare.fields.data.temporal.datetime_field import DatetimeField

        # A moment's time of day would be cut off.
        if isinstance(old_field, DatetimeField):
            return DayLimit(None, None, checks_time_of_day=True)
        return None

    def to_python(self, value: Any) -> datetime.date | None:
        if value is not None:
            value = self.get_date_value(value)
        return value

    def get_lookups(self) -> dict[str, FieldLookup]:
        """The generic lookups and the calendar parts of the value (``__year``, ``__month``, ...)."""
        # Local import: the filters package imports the fields package.
        from hare.query.filters.constants import CALENDAR_DATE_PART_LOOKUPS
        from hare.query.filters.lookups.field_lookups import FieldLookups

        return FieldLookups.get_date_parts(self, CALENDAR_DATE_PART_LOOKUPS)

    def get_date_value(self, value: Any) -> datetime.date:
        """Converts an accepted input into a date.

        Args:
            value: A date, a datetime (its date) or an ISO 8601 string.

        Returns:
            The date.

        Raises:
            ValidationError: The value has another type or doesn't denote a date.
        """
        if isinstance(value, datetime.datetime):
            # A datetime is itself a date subclass - narrowed, not passed through with its time.
            return value.date()
        if isinstance(value, datetime.date):
            return value
        if isinstance(value, str):
            TemporalValues.raise_if_not_full_date(self, value)
            return TemporalValues.parse_text(self, value, lambda text: parse_datetime(text).date())
        raise ValidationError(
            f"{self.model_field_name}: expected a date, a datetime or an ISO 8601 string, got {type(value).__name__}"
        )

    def to_db_value(self, value: Any, instance: type[Model] | Model | None) -> datetime.date | None:
        # Every accepted input is converted here, including a plain assignment or update().
        result = self.get_date_value(value) if value is not None else None
        self.validate(result)
        return result

    def get_unchanged_write_types(self, converter: Callable[..., Any] | None) -> tuple[type, ...] | None:
        # This class's to_db_value() writes a date as it is.
        field_class = type(self)
        if (
            converter is None
            and field_class.to_db_value is DateField.to_db_value
            and field_class.get_date_value is DateField.get_date_value
        ):
            return (datetime.date,)
        return None
