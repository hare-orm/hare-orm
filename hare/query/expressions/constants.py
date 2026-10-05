from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from hare.fields.data.numeric.decimal_field import DecimalField
from hare.fields.data.numeric.float_field import FloatField
from hare.fields.data.numeric.int_field import IntField
from hare.fields.field import Field
from hare.query.enums import Lookup
from hare.sql.types.sql_types import SqlTypes

#: `max_digits` of the shared DecimalField a Decimal-typed expression result (a literal, or
#: arithmetic involving one) is decoded through - generous enough to never reject a real result.
DECIMAL_OUTPUT_FIELD_MAX_DIGITS = 1000


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


#: Lookup suffixes whose value is a list of values - any other iterable is materialized into one.
LIST_LOOKUP_SUFFIXES = ("__in", "__not_in")
#: The lookup suffix of a two-bound range - a bound left None opens it on its side.
RANGE_LOOKUP_SUFFIX = "__range"
#: The lookup suffix of a JSON filter dict - its keys name the path and the operator.
JSON_FILTER_LOOKUP_SUFFIX = "__filter"
#: The lookup suffixes of JSON containment - a dialect may build the test from the compared value's
#: shape, which the plan key then holds.
JSON_CONTAINMENT_LOOKUP_SUFFIXES = ("__contains", "__contained_by")
#: The suffixes of filter keys whose value a plan found by the calls doesn't bind one to one,
#: whatever its type - a list lookup, and a JSON ``__filter`` dict described by its shape.
CALL_SIGNATURE_DESCRIBED_KEY_SUFFIXES = (*LIST_LOOKUP_SUFFIXES, JSON_FILTER_LOOKUP_SUFFIX)
#: The types of the most frequent plain values - no container, expression or SQL term - told
#: apart by their type alone, without an ``isinstance()`` check on a hot path.
PLAIN_VALUE_TYPES = frozenset({int, str, float, Decimal, bytes, date, datetime, time, timedelta, UUID})

#: The structure of an ``__in``/``__not_in`` list bound as one parameter, in place of its length.
LONG_IN_LIST_STRUCTURE = "one parameter"

#: The structure of a None argument - a CASE branch, a function or window argument - written as
#: NULL, never bound.
NULL_ARGUMENT_STRUCTURE = ("null",)

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

#: The origin a reference no later query can bind is recorded under (``PlanOrigins``) - a value
#: of a query built into another one keeping no plan, a SQL term binding a value no term holds: the
#: query keeps no plan.
UNBINDABLE_VALUE_ORIGIN = "unbindable"


#: The name of a ``JsonTable`` column - a Python identifier without ``__``.
JSON_TABLE_COLUMN_NAME_PATTERN = re.compile(r"(?!.*__)[A-Za-z_][A-Za-z0-9_]*")

#: The JSON path a ``JsonTable`` column reads when it names none - the item's key of its name.
JSON_TABLE_DEFAULT_COLUMN_PATH = "$.{name}"

#: The recursive CTE ``with_recursive()`` walks a relation in, its depth column, and the aliases of the
#: model's table in its base case and in its step.
RECURSIVE_ROWS_CTE_NAME = "hare_recursive_rows"

RECURSIVE_ROWS_DEPTH_COLUMN = "hare_depth"

RECURSIVE_ROWS_START_ALIAS = "hare_recursive_start"

RECURSIVE_ROWS_PREVIOUS_ALIAS = "hare_recursive_previous"

#: The largest ``with_recursive(max_depth=...)`` taken.
MAX_RECURSIVE_DEPTH = 100_000

#: The annotation a filter on a column of a named JOIN (``Lateral``, ``merge()``'s source row) reads the
#: column under.
NAMED_COLUMN_FILTER_ANNOTATION = "hare_named_column"
