from collections.abc import Sequence

from hare.dialects.postgresql.search.types import ScalarValue
from hare.query.expressions import Expression, ExpressionContext, ExpressionResult, F, Value
from hare.query.plans.description.plan_context import PlanContext
from hare.query.plans.description.plan_description import PlanDescription
from hare.sql.terms.base.term import Term


class SearchArguments:
    """The arguments of full-text search expressions."""

    @staticmethod
    def get_plan_description(
        value: Expression | Term | ScalarValue | Sequence[float] | Sequence[int] | str | None,
        context: PlanContext,
        *,
        treat_str_as_field: bool,
    ) -> PlanDescription | None:
        """Describes an argument the way ``get_result()`` resolves it.

        Args:
            value: The argument.
            context: The context it is resolved in.
            treat_str_as_field: Whether a string names a field.

        Returns:
            The description, None for a SQL term.
        """
        if isinstance(value, Expression):
            return value.get_plan_description(context)
        if isinstance(value, Term):
            return None
        if isinstance(value, str) and treat_str_as_field:
            return F(value).get_plan_description(context)
        return Value(value).get_plan_description(context)

    @staticmethod
    def get_result(
        value: Expression | Term | ScalarValue | Sequence[float] | Sequence[int] | str | None,
        expression_context: ExpressionContext,
        *,
        treat_str_as_field: bool,
    ) -> ExpressionResult:
        if isinstance(value, Expression):
            return value.get_result(expression_context)
        if isinstance(value, Term):
            return ExpressionResult(term=value)
        if isinstance(value, str) and treat_str_as_field:
            return F(value).get_result(expression_context)
        return Value(value).get_result(expression_context)
