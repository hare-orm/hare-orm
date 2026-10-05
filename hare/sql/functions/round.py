from __future__ import annotations

from typing import Any

from hare.sql.terms.functions.function import Function
from hare.sql.terms.values.literal_value import LiteralValue


class Round(Function):
    def __init__(self, term: Any, precision: int, alias: str | None = None) -> None:
        # The precision is a SQL literal, not a bind parameter - it's part of the SQL text.
        super().__init__("ROUND", term, LiteralValue(str(int(precision))), alias=alias)
