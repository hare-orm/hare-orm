from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any

from hare.fields.data.temporal.date_field import DateField
from hare.fields.data.temporal.datetime_field import DatetimeField
from hare.fields.data.temporal.time_delta_field import TimeDeltaField
from hare.fields.data.temporal.time_field import TimeField
from hare.fields.field import Field
from hare.query.expressions.enums import ArithmeticOperator, TemporalType

#: Field classes mapped to the temporal type of value they hold - checked in order.
TEMPORAL_FIELD_TYPES: tuple[tuple[type[Field[Any]], TemporalType], ...] = (
    (DatetimeField, TemporalType.DATETIME),
    (DateField, TemporalType.DATE),
    (TimeField, TemporalType.TIME),
    (TimeDeltaField, TemporalType.TIMEDELTA),
)

#: Python literal types mapped to their temporal type - checked in order, since `datetime` is a
#: `date` subclass and must be matched first.
TEMPORAL_LITERAL_TYPES: tuple[tuple[type, TemporalType], ...] = (
    (datetime, TemporalType.DATETIME),
    (date, TemporalType.DATE),
    (time, TemporalType.TIME),
    (timedelta, TemporalType.TIMEDELTA),
)

#: Type of the result of `left <connector> right` for every supported temporal combination - a
#: combination not listed here is rejected with a FieldError.
TEMPORAL_RESULT_TYPES: dict[tuple[ArithmeticOperator, TemporalType, TemporalType], TemporalType] = {
    (ArithmeticOperator.ADD, TemporalType.DATETIME, TemporalType.TIMEDELTA): TemporalType.DATETIME,
    (ArithmeticOperator.ADD, TemporalType.TIMEDELTA, TemporalType.DATETIME): TemporalType.DATETIME,
    (ArithmeticOperator.SUB, TemporalType.DATETIME, TemporalType.TIMEDELTA): TemporalType.DATETIME,
    (ArithmeticOperator.ADD, TemporalType.DATE, TemporalType.TIMEDELTA): TemporalType.DATE,
    (ArithmeticOperator.ADD, TemporalType.TIMEDELTA, TemporalType.DATE): TemporalType.DATE,
    (ArithmeticOperator.SUB, TemporalType.DATE, TemporalType.TIMEDELTA): TemporalType.DATE,
    (ArithmeticOperator.ADD, TemporalType.TIMEDELTA, TemporalType.TIMEDELTA): TemporalType.TIMEDELTA,
    (ArithmeticOperator.SUB, TemporalType.TIMEDELTA, TemporalType.TIMEDELTA): TemporalType.TIMEDELTA,
    (ArithmeticOperator.SUB, TemporalType.DATETIME, TemporalType.DATETIME): TemporalType.TIMEDELTA,
    (ArithmeticOperator.SUB, TemporalType.DATE, TemporalType.DATE): TemporalType.TIMEDELTA,
}
