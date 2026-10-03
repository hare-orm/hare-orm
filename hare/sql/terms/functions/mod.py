from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.sql.terms.base.term import Term

if TYPE_CHECKING:
    pass
from hare.sql.terms.functions.function import Function


class Mod(Function):
    """Remainder of ``term`` divided by ``modulus``.

    Args:
        term: The dividend.
        modulus: The divisor.
        alias: Optional alias for the term.
        integer: Both operands are integers - SQLite then uses its integer ``%`` operator, since
            its ``MOD()`` computes in floating point.
    """

    def __init__(self, term: Term, modulus: Any, alias: str | None = None, *, integer: bool = False) -> None:
        super().__init__("MOD", term, modulus, alias=alias)
        self.integer = integer
