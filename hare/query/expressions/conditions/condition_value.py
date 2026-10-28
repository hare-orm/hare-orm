from __future__ import annotations

import dataclasses
from typing import Any, ClassVar

from hare.fields.data.boolean_field import BooleanField
from hare.query.expressions.conditions.q import Q
from hare.query.expressions.conditions.query_modifier import QueryModifier
from hare.query.expressions.expression import Expression
from hare.query.expressions.expression_context import ExpressionContext
from hare.query.expressions.expression_result import ExpressionResult
from hare.query.expressions.subqueries.exists import Exists
from hare.query.plans.description.declared_plan_parts import DeclaredPlanParts
from hare.query.plans.enums import PlanPartType
from hare.sql.terms.bracket import Bracket
from hare.sql.terms.criteria.basic_criterion import BasicCriterion


class ConditionValue(Expression):
    """A condition as a value of each row - true where it holds, false where it doesn't, NULL where SQL's
    three-valued logic gives NULL: ``annotate(is_open=Q(closed_at__isnull=True))``. A ``Q`` given to
    ``annotate()``, ``alias()``, ``values()``, ``values_list()`` or ``update()`` is made one.

    Args:
        condition: A ``Q`` or ``Exists(...)`` condition, or several joined with ``&``/``|``/``~``.
    """

    #: The field every value is decoded through - a database may return 0/1; one shared instance.
    OUTPUT_FIELD = BooleanField(null=True)
    # The values of values()/values_list() and of instances are decoded through OUTPUT_FIELD.
    populate_field_object = True

    plan_parts: ClassVar[DeclaredPlanParts] = (("condition", PlanPartType.EXPRESSION),)

    def __init__(self, condition: Q | Exists) -> None:
        if not isinstance(condition, (Q, Exists)):
            raise TypeError(f"ConditionValue takes a Q or Exists(...) condition, got {type(condition).__name__}")
        self.condition = condition if isinstance(condition, Q) else Q(condition)

    @staticmethod
    def get_value_expression(value: Any) -> Any:
        """A value given where an expression is taken - a ``Q`` as its ``ConditionValue``, anything else as
        it is.

        Args:
            value: The value.

        Returns:
            The expression or value.
        """
        return ConditionValue(value) if isinstance(value, Q) else value

    def get_result(self, expression_context: ExpressionContext) -> ExpressionResult:
        # A condition read as a value may read a window function - it is evaluated in SELECT/ORDER BY.
        modifier: QueryModifier = self.condition.get_result(
            dataclasses.replace(expression_context, window_function_filter_allowed=True)
        )
        criterion = modifier._and_criterion() or BasicCriterion.always_true()
        # In parentheses: compared with a value (filter(is_open=True)) the condition stays one operand.
        return ExpressionResult(
            term=Bracket(criterion), joins=list(modifier.joins), output_field=ConditionValue.OUTPUT_FIELD
        )
