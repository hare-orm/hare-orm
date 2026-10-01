import re
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from hare.fields.base.field import Field
from hare.fields.data.numeric.decimal_field import DecimalField
from hare.fields.data.numeric.float_field import FloatField
from hare.fields.data.numeric.int_field import IntField
from hare.fields.data.temporal.date_field import DateField
from hare.fields.data.temporal.datetime_field import DatetimeField
from hare.fields.data.temporal.time_delta_field import TimeDeltaField
from hare.fields.data.temporal.time_field import TimeField
from hare.query.enums import Lookup
from hare.query.expressions.enums import ArithmeticOperator, NumericValueType, TemporalType
from hare.sql.sql_types import SqlTypes

#: Python type a text literal is converted to before it is combined, in arithmetic, with a column
#: holding numbers of each type.
NUMERIC_VALUE_TYPE_CONVERTERS: dict[NumericValueType, type] = {
    NumericValueType.INTEGER: int,
    NumericValueType.FLOAT: float,
    NumericValueType.DECIMAL: Decimal,
}

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

#: `max_digits` of the shared DecimalField a Decimal-typed expression result (a literal, or
#: arithmetic involving one) is decoded through - generous enough to never reject a real result.
DECIMAL_OUTPUT_FIELD_MAX_DIGITS = 1000

#: Arithmetic connectors whose Decimal result scale is the larger of the operands' scales -
#: `mul` adds them instead.
DECIMAL_MAX_SCALE_CONNECTORS: frozenset[ArithmeticOperator] = frozenset(
    {
        ArithmeticOperator.ADD,
        ArithmeticOperator.SUB,
        ArithmeticOperator.DIV,
        ArithmeticOperator.MOD,
        ArithmeticOperator.POW,
    }
)

#: Per numeric field class, the Python literal types `Coalesce` takes as a default without the
#: result losing the default's value when decoded through that field. Non-numeric fields aren't
#: listed: their defaults are trusted to match.
COALESCE_NUMERIC_FIELD_LITERAL_TYPES: dict[type[Field[Any]], tuple[type, ...]] = {
    IntField: (int,),
    FloatField: (int, float, Decimal),
    DecimalField: (int, Decimal),
}

#: The numeric main-field classes of `Coalesce` - a Decimal default next to one of them (or next to
#: a main argument of unknown type) is compared/aggregated as a number, never as text.
COALESCE_NUMERIC_FIELD_CLASSES: tuple[type[Field[Any]], ...] = tuple(COALESCE_NUMERIC_FIELD_LITERAL_TYPES)

#: Same as COALESCE_NUMERIC_FIELD_LITERAL_TYPES, for a default value that resolves to another
#: field (e.g. `F("other_column")`) instead of a bare literal.
COALESCE_NUMERIC_FIELD_DEFAULT_FIELD_CLASSES: dict[type[Field[Any]], tuple[type[Field[Any]], ...]] = {
    IntField: (IntField,),
    FloatField: (IntField, FloatField, DecimalField),
    DecimalField: (IntField, DecimalField),
}

#: Lookup suffixes whose value is a list of values - any other iterable is materialized into one.
LIST_LOOKUP_SUFFIXES = ("__in", "__not_in")

#: The structure of an ``__in``/``__not_in`` list bound as one parameter, in place of its length.
LONG_IN_LIST_STRUCTURE = "one parameter"

#: Lookup suffixes whose boolean value picks the SQL text (``IS NULL`` or ``IS NOT NULL``) rather
#: than a parameter.
ISNULL_LOOKUP_SUFFIXES = ("__isnull", "__not_isnull")

#: Lookups on a to-many relation's own name (``tags=``, ``tags__in=``, ...) resolved as the same
#: lookup on the related primary key (``tags__id=``, ``tags__id__in=``, ...).
TO_MANY_RELATION_SHORTCUT_LOOKUPS = (
    Lookup.EXACT,
    Lookup.NOT,
    Lookup.IN,
    Lookup.NOT_IN,
    Lookup.ISNULL,
    Lookup.NOT_ISNULL,
)

#: Lookups of ``TO_MANY_RELATION_SHORTCUT_LOOKUPS`` a many-to-many relation resolves on its own, as
#: a correlated ``[NOT] EXISTS``.
MANY_TO_MANY_EXISTS_LOOKUPS = (Lookup.ISNULL, Lookup.NOT_ISNULL)

#: Separator between a to-many relation path and the filter-call generation of the separate JOIN a
#: later `.filter()`/`.exclude()` call builds over that relation, in the paths the aggregate fan-out
#: check tracks - `tags#2` is a second JOIN over `tags`, distinct from the `tags` JOIN itself.
SEPARATE_FILTER_JOIN_PATH_SEPARATOR = "#"

#: Lookup suffix of a filter that, with the value `True`, keeps only the rows a JOIN found no
#: related row for.
ISNULL_LOOKUP_SUFFIX = "isnull"

#: Filter value types holding several values - an equality with one of them never narrows a
#: to-many JOIN to one related row.
MULTIPLE_VALUE_TYPES: tuple[type, ...] = (list, tuple, set, frozenset, dict)

#: Lookups comparing a date with a timestamp as the first moment of its day, as a date literal is.
DATE_TIMESTAMP_COMPARISON_LOOKUPS = frozenset({"", "exact", "not", "gt", "gte", "lt", "lte"})

#: A comparison lookup to the one giving the same result with its two sides swapped.
MIRRORED_COMPARISON_LOOKUPS: dict[str, str] = {
    "": "",
    "exact": "exact",
    "not": "not",
    "gt": "lt",
    "gte": "lte",
    "lt": "gt",
    "lte": "gte",
}

# The SQL type a float/Decimal literal operand is cast to: Postgres would otherwise infer the
# parameter's type from the other operand, and `F("n_int") + 0.5` would bind 0.5 as an integer.
LITERAL_CAST_SQL_TYPE: dict[type, str] = {
    float: SqlTypes.FLOAT,
    Decimal: SqlTypes.NUMERIC,
}

RAW_SQL_TOKEN_RE = re.compile(r"%%|%s")
