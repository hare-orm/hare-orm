from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.query.expressions.enums import ArithmeticOperator

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.query.expressions.base.combined_expression import CombinedExpression


class ArithmeticOperatorsMixin:
    """`+`/`-`/`*`/`/`/`%`/`**` and their reflected counterparts, each building a
    `CombinedExpression` through the class's own `_combine()` - listed before `Term` among a
    class's bases, they take precedence over the SQL-level arithmetic of `Term`.
    """

    def _combine(self, other: Any, connector: ArithmeticOperator, right_hand: bool) -> CombinedExpression:
        raise NotImplementedError()  # pragma: nocoverage

    def __neg__(self) -> CombinedExpression:
        return self._combine(-1, ArithmeticOperator.MUL, False)

    def __add__(self, other: Any) -> CombinedExpression:
        return self._combine(other, ArithmeticOperator.ADD, False)

    def __sub__(self, other: Any) -> CombinedExpression:
        return self._combine(other, ArithmeticOperator.SUB, False)

    def __mul__(self, other: Any) -> CombinedExpression:
        return self._combine(other, ArithmeticOperator.MUL, False)

    def __truediv__(self, other: Any) -> CombinedExpression:
        return self._combine(other, ArithmeticOperator.DIV, False)

    def __mod__(self, other: Any) -> CombinedExpression:
        return self._combine(other, ArithmeticOperator.MOD, False)

    def __pow__(self, other: Any) -> CombinedExpression:
        return self._combine(other, ArithmeticOperator.POW, False)

    def __radd__(self, other: Any) -> CombinedExpression:
        return self._combine(other, ArithmeticOperator.ADD, True)

    def __rsub__(self, other: Any) -> CombinedExpression:
        return self._combine(other, ArithmeticOperator.SUB, True)

    def __rmul__(self, other: Any) -> CombinedExpression:
        return self._combine(other, ArithmeticOperator.MUL, True)

    def __rtruediv__(self, other: Any) -> CombinedExpression:
        return self._combine(other, ArithmeticOperator.DIV, True)

    def __rmod__(self, other: Any) -> CombinedExpression:
        return self._combine(other, ArithmeticOperator.MOD, True)

    def __rpow__(self, other: Any) -> CombinedExpression:
        return self._combine(other, ArithmeticOperator.POW, True)
