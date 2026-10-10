from __future__ import annotations

from typing import Any

from hare.sql.functions.cast import Cast
from hare.sql.types.sql_types import SqlTypes


class NumericCast(Cast):
    """``CAST(term AS NUMERIC)`` for a Decimal compared or aggregated outside a column, on a dialect
    storing Decimals as text - a value without column affinity compares TEXT above every number.
    SQLite's NUMERIC isn't exact: a fractional value keeps ~15 significant digits.
    """

    def __init__(self, term: Any, alias: str | None = None) -> None:
        super().__init__(term, SqlTypes.NUMERIC, alias=alias)
