from __future__ import annotations

import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal, TypeVar, overload

from hare.exceptions import ValidationError
from hare.fields.constants import SENSITIVE_VALUE_PLACEHOLDER
from hare.fields.data.constants import INT64_MAX, INT64_MIN
from hare.fields.field import Field
from hare.sql.constants import MICROSECONDS_PER_DAY, MICROSECONDS_PER_SECOND

if TYPE_CHECKING:
    from hare.models import Model

TTimedelta = TypeVar("TTimedelta", datetime.timedelta, datetime.timedelta | None)


class TimeDeltaField(Field[TTimedelta]):
    """A duration - a ``timedelta`` stored as its whole number of microseconds in a ``BIGINT`` column."""

    field_type = datetime.timedelta
    SQL_TYPE = "BIGINT"

    @overload
    def __init__(self: TimeDeltaField[datetime.timedelta], *, null: Literal[False] = False, **kwargs: Any) -> None: ...

    @overload
    def __init__(self: TimeDeltaField[datetime.timedelta | None], *, null: Literal[True], **kwargs: Any) -> None: ...

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)

    def from_db_value(self, value: Any) -> datetime.timedelta | None:
        if value is None or isinstance(value, datetime.timedelta):
            return value
        return self.get_timedelta(value)

    def to_python(self, value: Any) -> datetime.timedelta | None:
        # A number is no duration - only one read from the column is microseconds.
        if value is not None:
            self.raise_if_not_timedelta(value)
        return value

    def raise_if_not_timedelta(self, value: Any) -> None:
        """Refuses a value given for the field that isn't a timedelta.

        Args:
            value: The value, not None.

        Raises:
            ValidationError: ``value`` isn't a timedelta.
        """
        if not isinstance(value, datetime.timedelta):
            raise ValidationError(
                f"{self.model_field_name}: expected a datetime.timedelta, got {self.get_value_for_message(value)}"
            )

    def get_timedelta(self, value: Any) -> datetime.timedelta:
        """Converts a number of microseconds read from the column to a timedelta.

        Args:
            value: The number - a Decimal or a float of an aggregate too.

        Returns:
            The timedelta.

        Raises:
            ValidationError: A number of microseconds out of timedelta's range.
        """
        try:
            if isinstance(value, (float, Decimal)):
                # Sum()/Avg() may return an exact numeric - a Decimal, rounded to whole microseconds.
                value = round(value)
            return datetime.timedelta(microseconds=value)
        except (OverflowError, ValueError) as error:
            validation_error = self.get_validation_error(
                error,
                value,
                f"{self.model_field_name}: {self.get_value_for_message(value)} is out of range for "
                "TimeDeltaField's microsecond storage",
            )
        raise validation_error

    @staticmethod
    def get_microseconds(value: datetime.timedelta) -> int:
        """Converts a timedelta to a whole number of microseconds.

        Args:
            value: The timedelta to convert.

        Returns:
            The signed total number of microseconds.
        """
        return (value.days * MICROSECONDS_PER_DAY) + (value.seconds * MICROSECONDS_PER_SECOND) + value.microseconds

    def to_db_value(self, value: datetime.timedelta | None, instance: type[Model] | Model | None) -> int | None:
        if value is not None:
            self.raise_if_not_timedelta(value)
        self.validate(value)

        if value is None:
            return None
        microseconds = self.get_microseconds(value)
        if not (INT64_MIN <= microseconds <= INT64_MAX):
            # A timedelta can hold far more than a BIGINT of microseconds - range checked here.
            shown_value = SENSITIVE_VALUE_PLACEHOLDER if self.sensitive else value
            raise ValidationError(
                f"{self.model_field_name}: {shown_value} is out of range for TimeDeltaField's microsecond storage"
            )
        return microseconds
