from __future__ import annotations

from hare.sql.terms.base.infix_operator import InfixOperator


class VectorInfixOperator(InfixOperator):
    """Renders one of pgvector's infix distance operators (``<->``/``<=>``/``<#>``) - these are
    genuine SQL infix operators, not named functions, so they can't be built via a plain
    ``Function("...")`` call."""
