from __future__ import annotations

from hare.sql.functions.json.json_value import JsonValue
from hare.sql.terms.functions.function import Function


class MathFunction(Function):
    """A math function - the function itself on Postgres, a hare UDF on SQLite."""


class TextFunction(Function):
    """A text function - the function itself on Postgres (its first argument read as text), SQLite's
    own function of the same semantics or a hare UDF on SQLite."""


class JsonComparand(JsonValue):
    """The JSON value of a typed expression, in the form the value at a JSON path compares with -
    ``jsonb`` on Postgres; on SQLite a number as it is and any other value as JSON text. NULL stays
    NULL."""
