from __future__ import annotations

from typing import Any

from hare.sql.terms.functions.function import Function
from hare.sql.terms.term import Term


class DecimalMod(Function):
    """Exact remainder of two decimals with at most ``scale`` digits after the decimal point - None
    when a scale isn't known.

    SQLite has no exact decimal type - its ``MOD()`` runs on doubles and misses remainders such
    as ``2.00 % 0.1`` - so there both operands are scaled to whole numbers first and the integer
    remainder is scaled back. Postgres's numeric ``MOD()`` is exact already.

    Args:
        term: The dividend.
        modulus: The divisor.
        scale: The larger of the two operands' scales, None when one isn't known.
        alias: Optional alias for the term.
    """

    def __init__(self, term: Term, modulus: Any, scale: int | None, alias: str | None = None) -> None:
        super().__init__("MOD", term, modulus, alias=alias)
        self.scale = scale
