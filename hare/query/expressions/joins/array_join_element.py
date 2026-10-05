from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from hare.query.expressions.expression import Expression

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.query.expressions.expression_context import ExpressionContext
    from hare.query.expressions.expression_result import ExpressionResult


class ArrayJoinElement(Expression):
    """What an ``ArrayJoin``'s name reads, as an annotation a filter compares - its JOIN is the
    ``ArrayJoin``'s own.

    Args:
        result: The element, or a path inside it.
    """

    #: Read through its ArrayJoin, which keeps no plan.
    plannable: ClassVar[bool] = False

    def __init__(self, result: ExpressionResult) -> None:
        self.result = result

    def get_result(self, expression_context: ExpressionContext) -> ExpressionResult:
        return self.result
