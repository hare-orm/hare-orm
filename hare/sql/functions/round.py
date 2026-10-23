from __future__ import annotations

from typing import Any

from hare.sql.exceptions import QueryException
from hare.sql.terms.functions.function import Function
from hare.sql.terms.values.literal_value import LiteralValue


class Round(Function):
    """``ROUND(x, precision)``."""

    def __init__(self, term: Any, precision: int, alias: str | None = None) -> None:
        if isinstance(precision, bool) or not isinstance(precision, int):
            raise QueryException(f"ROUND takes an int precision, got {precision!r}")
        # The precision is a SQL literal, not a bind parameter - it's part of the SQL text.
        super().__init__("ROUND", term, LiteralValue(str(int(precision))), alias=alias)
        self.precision = int(precision)
        #: Whether the value rounded is a Decimal - a dialect rounding a double rounds it otherwise.
        self.rounds_decimal = False
