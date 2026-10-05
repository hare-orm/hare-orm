from __future__ import annotations

import datetime
from typing import TYPE_CHECKING, Any, Literal, TypeVar, overload

from hare.exceptions import ValidationError
from hare.fields.data.temporal.parse_datetime import parse_datetime
from hare.fields.db_defaults.now import Now
from hare.fields.enums import NowValueType
from hare.fields.field import Field

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model
    from hare.query.filters.lookups.field_lookup import FieldLookup
from hare.fields.data.temporal.temporal_values import TemporalValues

TDate = TypeVar("TDate", datetime.date, datetime.date | None)
DateFieldQueryValueType = TypeVar("DateFieldQueryValueType", datetime.date, int, float, str)


class DateField(Field[TDate]):
    """
    Date field.
    """

    field_type = datetime.date

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

    def to_python(self, value: Any) -> datetime.date | None:
        if value is not None:
            value = self.get_date_value(value)
        return value

    def get_lookups(self) -> dict[str, FieldLookup]:
        """The generic lookups and the date parts of the value (``__year``, ``__hour``, ...)."""
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

    def to_db_value(
        self, value: DateFieldQueryValueType | None, instance: type[Model] | Model
    ) -> DateFieldQueryValueType | None:
        if value is not None:
            # A plain attribute assignment or an update() never went through from_db_value() -
            # every accepted input is converted (or rejected) here, whichever path wrote it.
            value = self.get_date_value(value)  # type: ignore[assignment]
        self.validate(value)
        return value
