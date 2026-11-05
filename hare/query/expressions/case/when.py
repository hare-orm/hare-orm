from __future__ import annotations

import dataclasses
from typing import Any, ClassVar

from hare.dialects.enums import ParameterPosition
from hare.gis.geometries.geometry import Geometry
from hare.query.expressions.bound_expression import BoundExpression
from hare.query.expressions.conditions.q import Q
from hare.query.expressions.conditions.query_modifier import QueryModifier
from hare.query.expressions.expression import Expression
from hare.query.expressions.expression_context import ExpressionContext
from hare.query.expressions.expression_result import ExpressionResult
from hare.query.expressions.structure.declarations import ExpressionChild
from hare.query.expressions.structure.enums import ArgumentRole
from hare.query.expressions.subqueries.exists import Exists
from hare.query.expressions.value import Value
from hare.query.plans.description.declared_plan_parts import DeclaredPlanParts
from hare.query.plans.enums import PlanPartType
from hare.sql.terms.case.when_then import WhenThen
from hare.sql.terms.criteria.basic_criterion import BasicCriterion


class When(Expression):
    """A branch of ``Case``: its value when its condition holds.

    Args:
        args: The conditions - ``Q`` objects or ``Exists(...)``.
        kwargs: Conditions as ``filter()`` takes them.
        then: The branch's expression, SQL term or Python literal. Literal support follows
            the value field and dialect, including custom field types.
        negate: Whether the branch holds when the conditions don't.
    """

    plan_parts: ClassVar[DeclaredPlanParts] = (
        ("conditions", PlanPartType.EXPRESSIONS),
        ("then", PlanPartType.ARGUMENT),
        ("args", PlanPartType.NONE),
        ("kwargs", PlanPartType.NONE),
        ("negate", PlanPartType.NONE),
    )

    def __init__(
        self,
        *args: Q | Exists,
        then: Any,
        negate: bool = False,
        **kwargs: Any,
    ) -> None:
        self.args = args
        # A geometry is written as Value() writes it - a driver binds none.
        self.then = Value(then) if isinstance(then, Geometry) else then
        self.negate = negate
        self.kwargs = kwargs
        #: The conditions the branch matches on - made once, the same objects for its plan and its build.
        self.conditions = self._get_q_objects()

    def get_expression_children(self) -> tuple[ExpressionChild, ...]:
        """Separate lookup keys from the branch's literal or expression result."""
        return (
            ExpressionChild("args", self.args),
            ExpressionChild("kwargs", self.kwargs, ArgumentRole.FILTERS),
            ExpressionChild("then", self.then),
        )

    def rebuild_expression(self, children: tuple[ExpressionChild, ...]) -> When:
        """Recompute conditions from transformed inputs, retaining negation."""
        values = {child.argument: child.value for child in children}
        return type(self)(*values["args"], then=values["then"], negate=self.negate, **values["kwargs"])

    def _get_q_objects(self) -> list[Q]:
        """The conditions this branch matches on - negated as one whole, like ``~Q(...)``.

        Returns:
            The Q objects ANDed together into the branch's condition.

        Raises:
            TypeError: A positional argument isn't a Q object or an Exists(...) condition.
        """
        q_objects = []
        for arg in self.args:
            if isinstance(arg, Exists):
                arg = Q(arg)
            if not isinstance(arg, Q):
                raise TypeError("expected Q objects or Exists(...) conditions as args")
            q_objects.append(arg)
        for key, value in self.kwargs.items():
            q_objects.append(Q(**{key: value}))
        if not self.negate or not q_objects:
            return q_objects
        combined_condition = q_objects[0]
        for q_object in q_objects[1:]:
            combined_condition &= q_object
        return [~combined_condition]

    def get_result(self, expression_context: ExpressionContext) -> ExpressionResult:
        """Build this branch's condition and value, including the joins each needs.

        A standalone branch types literals by their own field and dialect. Case binds
        its branches separately when a common result type must be chosen first.
        """
        condition_result = self.get_condition_result(expression_context)
        value_binding = BoundExpression.prepare_argument(
            self, "then", self.then, expression_context, label="a When(then=...) branch"
        )
        then_result = value_binding.lower(
            expression_context,
            encoder=Value.get_literal_encoder(self.then, expression_context.dialect),
            position=ParameterPosition.CASE_BRANCH,
        )
        return ExpressionResult(
            term=WhenThen(condition_result.term, then_result.term),
            joins=ExpressionResult.dedup_joins(condition_result.joins, then_result.joins),
            output_field=then_result.output_field,
        )

    def get_condition_result(self, expression_context: ExpressionContext) -> ExpressionResult:
        """Bind only the branch condition; its value can be typed before lowering separately."""
        condition_expression_context = dataclasses.replace(expression_context, window_function_filter_allowed=True)
        modifier = QueryModifier()
        for node in self.conditions:
            modifier &= node.get_result(condition_expression_context)

        # Both criteria: a condition on an aggregate annotation lands in having_criterion, and a
        # CASE WHEN has one condition.
        condition = modifier._and_criterion()
        if not condition:
            # A When with no conditions is always true - 1=1, as SQL has no portable TRUE.
            condition = BasicCriterion.always_true()
        return ExpressionResult(term=condition, joins=list(modifier.joins))
