from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.query.expressions.enums import ArithmeticOperator
from hare.sql.terms.base.term import Term

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.query.expressions.base.combined_expression import CombinedExpression

    pass
from hare.query.expressions.base.arithmetic_operators_mixin import ArithmeticOperatorsMixin
from hare.query.expressions.base.expression import Expression
from hare.query.expressions.base.value import Value


class ArithmeticExpressionMixin(ArithmeticOperatorsMixin, Expression, abstract=True):
    """Arithmetic for an `Expression` subclass (`ArithmeticOperatorsMixin`) - shared by `F`,
    `OuterRef`, `CombinedExpression`, `Function` (functions and aggregates), `Case` and `Subquery`.
    """

    @staticmethod
    def get_operand_expression(value: Any) -> Expression:
        """The expression an arithmetic operand stands for.

        Args:
            value: The operand - an expression, a raw SQL term or a Python literal.

        Returns:
            The expression itself, a raw SQL term embedded as is, or a literal bound as a parameter.
        """
        # Imported here: the modules import each other.
        from hare.query.expressions.base.term_expression import TermExpression

        if isinstance(value, Expression):
            return value
        if isinstance(value, Term):
            return TermExpression(value)
        return Value(value)

    def _combine(self, other: Any, connector: ArithmeticOperator, right_hand: bool) -> CombinedExpression:
        # Imported here: the modules import each other.
        from hare.query.expressions.base.combined_expression import CombinedExpression

        other = self.get_operand_expression(other)

        if right_hand:
            return CombinedExpression(other, connector, self)
        return CombinedExpression(self, connector, other)
