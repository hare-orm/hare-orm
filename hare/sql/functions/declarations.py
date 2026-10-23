from __future__ import annotations

from hare.sql.functions.json.json_value import JsonValue
from hare.sql.terms.functions.function import Function


class MathFunction(Function):
    """A math function - the database's own, or hare's where the dialect registers one."""


class TextFunction(Function):
    """A text function - the database's own (its first argument read as text), or hare's where the
    dialect registers one."""


class JsonComparand(JsonValue):
    """The JSON value of a typed expression, in the form the value at a JSON path compares with -
    the dialect's JSON form of it (a number as it is and any other value as JSON text, say). NULL stays
    NULL."""
