from __future__ import annotations

from typing import Any

from hare.sql.terms.functions.function import Function
from hare.sql.terms.term import Term


class FloatMod(Function):
    """Floating-point remainder of ``term`` divided by ``modulus``, with the dividend's sign.

    A dialect with no ``MOD()`` for double precision computes it as ``x - y * TRUNC(x / y)``; another
    writes its own floating-point ``MOD()``.

    Args:
        term: The dividend.
        modulus: The divisor.
        alias: Optional alias for the term.
    """

    def __init__(self, term: Term, modulus: Any, alias: str | None = None) -> None:
        super().__init__("MOD", term, modulus, alias=alias)

    requires_dialect_renderer = True
