from __future__ import annotations

import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal, TypeVar, overload

from hare.exceptions import ValidationError
from hare.fields.constants import SENSITIVE_VALUE_PLACEHOLDER
from hare.fields.data.constants import INT64_MAX, INT64_MIN
from hare.fields.field import Field
from hare.sql.constants import MICROSECONDS_PER_DAY, MICROSECONDS_PER_SECOND

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model

TTimedelta = TypeVar("TTimedelta", datetime.timedelta, datetime.timedelta | None)


class TimeDeltaField(Field[TTimedelta]):
    """
    A field for storing time differences.
    """

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
        if isinstance(value, (float, Decimal)):
            # Sum()/Avg() return NUMERIC on Postgres - a Decimal, rounded to whole microseconds.
            value = round(value)
        return datetime.timedelta(microseconds=value)

    def to_python(self, value: Any) -> datetime.timedelta | None:
        # An assigned number is microseconds, as a stored one is.
        return self.get_timedelta(value)

    def get_timedelta(self, value: Any) -> datetime.timedelta | None:
        """Converts a written value (a timedelta, or a number of microseconds) to a timedelta.

        Args:
            value: The written value.

        Returns:
            The timedelta, or None.

        Raises:
            ValidationError: A number of microseconds is out of timedelta's range.
        """
        try:
            return self.from_db_value(value)
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

    def to_db_value(self, value: datetime.timedelta | None, instance: type[Model] | Model) -> int | None:
        if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
            # A raw number is a whole number of microseconds on every write path, the same
            # convention Model(duration=5_000_000) already applies on construction.
            value = self.get_timedelta(value)
        self.validate(value)

        if value is None:
            return None
        if not isinstance(value, datetime.timedelta):
            # A value that isn't a timedelta raises ValidationError.
            raise ValidationError(
                f"{self.model_field_name}: expected a datetime.timedelta, got {self.get_value_for_message(value)}"
            )
        microseconds = self.get_microseconds(value)
        if not (INT64_MIN <= microseconds <= INT64_MAX):
            # A timedelta can hold far more than a BIGINT of microseconds - range checked here.
            shown_value = SENSITIVE_VALUE_PLACEHOLDER if self.sensitive else value
            raise ValidationError(
                f"{self.model_field_name}: {shown_value} is out of range for TimeDeltaField's microsecond storage"
            )
        return microseconds
