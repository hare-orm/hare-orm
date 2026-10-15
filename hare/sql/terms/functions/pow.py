from __future__ import annotations

from hare.sql.terms.functions.function import Function
from hare.sql.terms.term import Term


class Pow(Function):
    def __init__(self, term: Term, exponent: float, alias: str | None = None) -> None:
        super().__init__("POWER", term, exponent, alias=alias)
