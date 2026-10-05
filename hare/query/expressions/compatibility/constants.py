from __future__ import annotations

import datetime
import decimal
import uuid
from typing import Any

from hare.fields.data.boolean_field import BooleanField
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

#: Same as COALESCE_NUMERIC_FIELD_LITERAL_TYPES, for a default value that resolves to another
#: field (e.g. `F("other_column")`) instead of a bare literal.
COALESCE_NUMERIC_FIELD_DEFAULT_FIELD_CLASSES: dict[type[Field[Any]], tuple[type[Field[Any]], ...]] = {
    IntField: (IntField,),
    FloatField: (IntField, FloatField, DecimalField),
    DecimalField: (IntField, DecimalField),
}

#: The types of values a CASE/COALESCE/NULLIF combines only among themselves, by the fields holding
#: them - a number with text, a boolean with a number, ... is refused, as PostgreSQL refuses it.
#: Numbers are told by ``NumericTyping``; a field of no type here takes part in no check.
RESULT_VALUE_TYPE_FIELD_CLASSES: tuple[tuple[str, tuple[type[Field[Any]], ...]], ...] = (
    ("text", (CharField, TextField)),
    ("a boolean", (BooleanField,)),
    ("a date or a moment", (DateField, DatetimeField)),
    ("a time of day", (TimeField,)),
    ("a duration", (TimeDeltaField,)),
    ("a UUID", (UUIDField,)),
)
#: The same types by the literals of them - a boolean before a number, which it is in Python.
RESULT_VALUE_TYPE_LITERAL_TYPES: tuple[tuple[str, tuple[type, ...]], ...] = (
    ("a boolean", (bool,)),
    ("a number", (int, float, decimal.Decimal)),
    ("text", (str,)),
    ("a date or a moment", (datetime.date,)),
    ("a time of day", (datetime.time,)),
    ("a duration", (datetime.timedelta,)),
    ("a UUID", (uuid.UUID,)),
)
#: The type of values every function of numbers refuses - what tells such a function from another.
TEXT_VALUE_TYPE = "text"
#: The types of values no arithmetic applies to - PostgreSQL has none for them.
ARITHMETIC_REFUSED_VALUE_TYPES = frozenset({"text", "a boolean"})
#: The types of values that aren't numbers - a numeric function or a statistic takes none of them.
NON_NUMBER_VALUE_TYPES = frozenset({"text", "a boolean", "a date or a moment", "a time of day", "a duration"})
#: The types of values that aren't text - a text function takes none of them.
NON_TEXT_VALUE_TYPES = frozenset({"a number", "a boolean", "a date or a moment", "a time of day", "a duration"})
#: The types of values ``SUM``/``AVG`` take none of - numbers and durations are added up.
UNSUMMED_VALUE_TYPES = frozenset({"text", "a boolean", "a date or a moment", "a time of day"})
#: The types of values ``MAX``/``MIN`` take none of - PostgreSQL has them for no boolean and no UUID.
UNORDERED_VALUE_TYPES = frozenset({"a boolean", "a UUID"})
