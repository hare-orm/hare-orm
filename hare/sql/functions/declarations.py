from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.sql.functions.cast import Cast
from hare.sql.functions.json_value import JsonValue
from hare.sql.functions.statistic import Statistic
from hare.sql.sql_types import SqlTypes
from hare.sql.terms.functions.function import Function

if TYPE_CHECKING:  # pragma: nocoverage
    pass


class MathFunction(Function):
    """A math function - the function itself on Postgres, a hare UDF on SQLite."""


class TextFunction(Function):
    """A text function - the function itself on Postgres (its first argument read as text), SQLite's
    own function of the same semantics or a hare UDF on SQLite."""


class AnyValue(Function):
    """`ANY(term)` - Postgres' array-membership form, used as the right operand of `=`/`<>`
    (`col = ANY($1::int[])`) in place of `col IN ($1,$2,...)` for a large value list."""

    def __init__(self, term: Any, alias: str | None = None) -> None:
        super().__init__("ANY", term, alias=alias)


class BooleanAsText(Function):
    """A boolean term rendered as ``'true'``/``'false'`` text on every dialect (NULL stays NULL).

    SQLite has no boolean type and would otherwise render a stored boolean as ``1``/``0``.
    """

    requires_dialect_renderer = True

    def __init__(self, term: Any, alias: str | None = None) -> None:
        super().__init__("CAST", term, alias=alias)


# Null Functions
class Coalesce(Function):
    def __init__(self, term: Any, *default_values, **kwargs) -> None:
        super().__init__("COALESCE", term, *default_values, **kwargs)


class Concat(Function):
    def __init__(self, *terms, **kwargs) -> None:
        super().__init__("CONCAT", *terms, **kwargs)


class Date(Function):
    def __init__(self, term: Any, alias: str | None = None) -> None:
        super().__init__("DATE", term, alias=alias)


class JsonComparand(JsonValue):
    """The JSON value of a typed expression, in the form the value at a JSON path compares with -
    ``jsonb`` on Postgres; on SQLite a number as it is and any other value as JSON text. NULL stays
    NULL."""


class JsonObject(Function):
    """``JSONB_BUILD_OBJECT(key, value, ...)`` - SQLite's ``json_object``."""

    requires_dialect_renderer = True

    def __init__(self, *arguments: Any, alias: str | None = None) -> None:
        super().__init__("JSONB_BUILD_OBJECT", *arguments, alias=alias)


class JsonSortKey(Function):
    """A JSON value ordered as Postgres orders ``jsonb`` - on SQLite its JSON text under a collation
    comparing JSON values, on Postgres the value itself."""

    requires_dialect_renderer = True

    def __init__(self, term: Any, alias: str | None = None) -> None:
        super().__init__("JSON", term, alias=alias)


class Lower(Function):
    def __init__(self, term: Any, alias: str | None = None) -> None:
        super().__init__("LOWER", term, alias=alias)


class Upper(Function):
    def __init__(self, term: Any, alias: str | None = None) -> None:
        super().__init__("UPPER", term, alias=alias)


# Date/Time Functions
class Now(Function):
    def __init__(self, alias: str | None = None) -> None:
        super().__init__("NOW", alias=alias)


class NumericCast(Cast):
    """``CAST(term AS NUMERIC)`` for a Decimal compared or aggregated outside a column, on a dialect
    storing Decimals as text - a value without column affinity compares TEXT above every number.
    SQLite's NUMERIC isn't exact: a fractional value keeps ~15 significant digits.
    """

    def __init__(self, term: Any, alias: str | None = None) -> None:
        super().__init__(term, SqlTypes.NUMERIC, alias=alias)


# Arithmetic Functions
class StdDev(Statistic):
    """The sample standard deviation - Postgres's ``STDDEV``."""

    def __init__(self, term: Any, alias: str | None = None) -> None:
        super().__init__("STDDEV_SAMP", term, alias=alias)
