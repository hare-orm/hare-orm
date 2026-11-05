from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.query.expressions.expression import Expression
from hare.query.expressions.expression_result import ExpressionResult

if TYPE_CHECKING:
    from hare.fields.field import Field
    from hare.query.expressions.expression_context import ExpressionContext
    from hare.sql.terms.term import Term


class ProjectedValue(Expression):
    """A bound derived-table column with the value field of its selected expression."""

    plannable = False

    def __init__(self, term: Term, output_field: Field[Any] | None) -> None:
        self.term = term
        self.output_field = output_field

    def get_result(self, expression_context: ExpressionContext) -> ExpressionResult:
        return ExpressionResult(term=self.term, output_field=self.output_field)
