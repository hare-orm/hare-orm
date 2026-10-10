from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.query.expressions.arithmetic.arithmetic_operators import ArithmeticOperators
from hare.query.expressions.enums import ArithmeticOperator
from hare.query.expressions.expression import Expression
from hare.query.expressions.value import Value
from hare.sql.terms.term import Term

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.query.expressions.arithmetic.combined_expression import CombinedExpression


class CombinableExpression(ArithmeticOperators, Expression, abstract=True):
    """Arithmetic for an `Expression` subclass (`ArithmeticOperators`) - shared by `F`,
    `OuterReference`, `CombinedExpression`, `Function` (functions and aggregates), `Case` and `Subquery`.
    """

    @staticmethod
    def get_operand_expression(value: Any) -> Expression:
        """The expression an arithmetic operand stands for.

        Args:
            value: The operand - an expression, a raw SQL term or a Python literal.

        Returns:
            The expression itself, a raw SQL term embedded as is, or a literal bound as a parameter.
        """
        if isinstance(value, Expression):
            return value
        if isinstance(value, Term):
            # Imported here: the modules import each other.
            from hare.query.expressions.term_expression import TermExpression

            return TermExpression(value)
        return Value(value)

    def _combine(self, other: Any, connector: ArithmeticOperator, right_hand: bool) -> CombinedExpression:
        # Imported here: the modules import each other.
        from hare.query.expressions.arithmetic.combined_expression import CombinedExpression

        other = self.get_operand_expression(other)

        if right_hand:
            return CombinedExpression(other, connector, self)
        return CombinedExpression(self, connector, other)
