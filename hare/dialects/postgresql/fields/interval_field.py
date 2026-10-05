from __future__ import annotations

import datetime
from typing import TYPE_CHECKING, Any, TypeVar

from hare.dialects.enums import DialectName
from hare.exceptions import ValidationError
from hare.fields.field import Field

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model

TTimedelta = TypeVar("TTimedelta", datetime.timedelta, datetime.timedelta | None)


class IntervalField(Field[TTimedelta]):
    """An ``interval`` - a duration, held as a timedelta. PostgreSQL keeps the months of an interval
    apart from its days; one written by SQL (``'1 month'``) is read as 30 days a month and 365 days a
    year, as asyncpg converts it. ``TimeDeltaField`` writes a ``bigint`` of microseconds.
    """

    SQL_TYPE = "INTERVAL"
    SUPPORTED_DIALECTS = frozenset({DialectName.POSTGRESQL})
    field_type = datetime.timedelta

    def to_python(self, value: Any) -> datetime.timedelta | None:
        if value is not None and not isinstance(value, datetime.timedelta):
            raise ValidationError(f"{self.model_field_name}: expected a timedelta, got {type(value).__name__}")
        return value

    def to_db_value(self, value: Any, instance: type[Model] | Model) -> Any:
        return super().to_db_value(self.to_python(value), instance)
