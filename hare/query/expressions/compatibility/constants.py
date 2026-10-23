from __future__ import annotations

import datetime
import decimal
import uuid
from typing import Any

from hare.fields.data.boolean_field import BooleanField
from hare.fields.data.constants import JSON_VALUE_TYPE
from hare.fields.data.numeric.decimal_field import DecimalField
from hare.fields.data.numeric.float_field import FloatField
from hare.fields.data.numeric.int_field import IntField
from hare.fields.data.temporal.date_field import DateField
from hare.fields.data.temporal.datetime_field import DatetimeField
from hare.fields.data.temporal.time_delta_field import TimeDeltaField
from hare.fields.data.temporal.time_field import TimeField
from hare.fields.data.text.char_field import CharField
from hare.fields.data.text.text_field import TextField
from hare.fields.data.uuid_field import UUIDField
from hare.fields.field import Field

#: Per numeric field class, the Python literal types `Coalesce` takes as a default without the
#: result losing the default's value when decoded through that field. Non-numeric fields aren't
#: listed: their defaults are trusted to match.
COALESCE_NUMERIC_FIELD_LITERAL_TYPES: dict[type[Field[Any]], tuple[type, ...]] = {
    IntField: (int,),
    FloatField: (int, float, decimal.Decimal),
    DecimalField: (int, decimal.Decimal),
}
#: Same as COALESCE_NUMERIC_FIELD_LITERAL_TYPES, for a default value that resolves to another
#: field (e.g. `F("other_column")`) instead of a bare literal.
COALESCE_NUMERIC_FIELD_DEFAULT_FIELD_CLASSES: dict[type[Field[Any]], tuple[type[Field[Any]], ...]] = {
    IntField: (IntField,),
    FloatField: (IntField, FloatField, DecimalField),
    DecimalField: (IntField, DecimalField),
}
#: The numeric main-field classes of `Coalesce` - a Decimal default next to one of them (or next to
#: a main argument of unknown type) is compared/aggregated as a number, never as text.
COALESCE_NUMERIC_FIELD_CLASSES: tuple[type[Field[Any]], ...] = tuple(COALESCE_NUMERIC_FIELD_LITERAL_TYPES)

#: The names of the types of values, as refusals name them. The type of values every function of numbers
#: refuses is text - what tells such a function from another.
TEXT_VALUE_TYPE = "text"
BOOLEAN_VALUE_TYPE = "a boolean"
NUMBER_VALUE_TYPE = "a number"
#: Dates and moments, one type.
DATE_OR_MOMENT_VALUE_TYPE = "a date or a moment"
TIME_OF_DAY_VALUE_TYPE = "a time of day"
DURATION_VALUE_TYPE = "a duration"
UUID_VALUE_TYPE = "a UUID"
#: The type of a range's values - a database runs a function of ranges by a text function's name
#: (``upper()`` of a range is its upper bound).
RANGE_VALUE_TYPE = "a range"

#: The types of values a CASE/COALESCE/NULLIF combines only among themselves, by the fields holding
#: them - a number with text, a boolean with a number, ... is refused: the result is read through one
#: field, which reads none of the other's values. Numbers are told by ``NumericTyping``; a field of no
#: type here takes part in no check.
RESULT_VALUE_TYPE_FIELD_CLASSES: tuple[tuple[str, tuple[type[Field[Any]], ...]], ...] = (
    (TEXT_VALUE_TYPE, (CharField, TextField)),
    (BOOLEAN_VALUE_TYPE, (BooleanField,)),
    (DATE_OR_MOMENT_VALUE_TYPE, (DateField, DatetimeField)),
    (TIME_OF_DAY_VALUE_TYPE, (TimeField,)),
    (DURATION_VALUE_TYPE, (TimeDeltaField,)),
    (UUID_VALUE_TYPE, (UUIDField,)),
)
#: The same types by the literals of them - a boolean before a number, which it is in Python.
RESULT_VALUE_TYPE_LITERAL_TYPES: tuple[tuple[str, tuple[type, ...]], ...] = (
    (BOOLEAN_VALUE_TYPE, (bool,)),
    (NUMBER_VALUE_TYPE, (int, float, decimal.Decimal)),
    (TEXT_VALUE_TYPE, (str,)),
    (DATE_OR_MOMENT_VALUE_TYPE, (datetime.date,)),
    (TIME_OF_DAY_VALUE_TYPE, (datetime.time,)),
    (DURATION_VALUE_TYPE, (datetime.timedelta,)),
    (UUID_VALUE_TYPE, (uuid.UUID,)),
)
#: The types of values no arithmetic of hare applies to.
ARITHMETIC_REFUSED_VALUE_TYPES = frozenset({TEXT_VALUE_TYPE, BOOLEAN_VALUE_TYPE})
#: The refusal of arithmetic on a JSON value - a JSON path (``F("data__count")``) is cast to a number first.
JSON_ARITHMETIC_MESSAGE = "Cannot use arithmetic on a JSON value - convert it to a number with Cast()"
#: The types of values that aren't numbers - a numeric function or a statistic takes none of them.
NON_NUMBER_VALUE_TYPES = frozenset(
    {
        TEXT_VALUE_TYPE,
        BOOLEAN_VALUE_TYPE,
        DATE_OR_MOMENT_VALUE_TYPE,
        TIME_OF_DAY_VALUE_TYPE,
        DURATION_VALUE_TYPE,
        JSON_VALUE_TYPE,
    }
)
#: The types of values that aren't text - a text function takes none of them.
NON_TEXT_VALUE_TYPES = frozenset(
    {
        NUMBER_VALUE_TYPE,
        BOOLEAN_VALUE_TYPE,
        DATE_OR_MOMENT_VALUE_TYPE,
        TIME_OF_DAY_VALUE_TYPE,
        DURATION_VALUE_TYPE,
        RANGE_VALUE_TYPE,
        JSON_VALUE_TYPE,
    }
)
#: The types of values ``SUM``/``AVG`` take none of - numbers and durations are added up.
UNSUMMED_VALUE_TYPES = frozenset(
    {TEXT_VALUE_TYPE, BOOLEAN_VALUE_TYPE, DATE_OR_MOMENT_VALUE_TYPE, TIME_OF_DAY_VALUE_TYPE, JSON_VALUE_TYPE}
)
