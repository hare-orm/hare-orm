from __future__ import annotations

import dataclasses
from collections.abc import Callable
from typing import Any, cast

from hare.dialects.enums import ParameterPosition
from hare.fields.base.field import Field
from hare.fields.encrypted.encrypted_field_mixin import EncryptedFieldMixin
from hare.query.expressions.base.expression import Expression
from hare.query.expressions.base.expression_context import ExpressionContext
from hare.query.expressions.base.expression_result import ExpressionResult
from hare.query.expressions.base.value import Value
from hare.query.expressions.case_branch_value import CaseBranchValue
from hare.query.expressions.enums import ValueRefOrigin
from hare.query.expressions.exists import Exists
from hare.query.expressions.modifier import QueryModifier
from hare.query.expressions.q import Q
from hare.query.expressions.value_refs.literal_value_ref import LiteralValueRef
from hare.query.expressions.when_then import WhenThen
from hare.query.plans.description.plan_context import PlanContext
from hare.query.plans.description.plan_description import PlanDescription
from hare.sql.enums import Equality
from hare.sql.terms.base.term import Term
from hare.sql.terms.base.value_wrapper import ValueWrapper
from hare.sql.terms.criteria.basic_criterion import BasicCriterion


class When(Expression):
    """
    When expression.

    Args:
        args: Q objects or Exists(...) conditions
        kwargs: keyword criterion like filter
        then: value for criterion
        negate: false (default)
    """

    def __init__(
        self,
        *args: Q | Exists,
        then: CaseBranchValue,
        negate: bool = False,
        **kwargs: Any,
    ) -> None:
        self.args = args
        self.then = then
        self.negate = negate
        self.kwargs = kwargs

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

    def get_plan_description(self, context: PlanContext) -> PlanDescription | None:
        """The conditions, then the ``then=`` value - a literal one by its type, which decides the
        field the result is decoded through.

        Args:
            context: The context the branch is resolved in.

        Returns:
            The description, None when a condition or the value keeps no plan - a None value
            renders ``NULL``, not a parameter.
        """
        q_objects = self._get_q_objects()
        return PlanDescription.combine(
            (When, len(q_objects)),
            (
                *(q_object.get_plan_description(context) for q_object in q_objects),
                self.get_argument_plan_description(self.then, context),
            ),
        )

    @staticmethod
    def get_value_result(
        value: Any, encoder: Callable[[Any], Any] | None, expression_context: ExpressionContext, label: str
    ) -> ExpressionResult:
        """Resolves a CASE branch value - an expression, or a bare literal bound as a parameter.

        Args:
            value: The ``then=``/``default=`` value.
            encoder: Converts a literal into its bound form first, when given.
            expression_context: The context being resolved.
            label: Names the branch in an encrypted-field error.

        Returns:
            The branch term, its joins and the field its value is decoded through. A numeric
            literal is cast to its own SQL type on Postgres, which otherwise infers it from the
            other branches (and truncates a fraction next to an integer column).
        """
        dialect = expression_context.dialect
        joins: list[Any] = []
        output_field: Field[Any] | None = None
        if isinstance(value, Expression):
            result = (
                value.get_encoded_result(expression_context, encoder)
                if isinstance(value, Value) and encoder is not None
                else value.get_result(expression_context)
            )
            term = result.term
            joins = list(result.joins)
            output_field = value.get_value_field(result)
            EncryptedFieldMixin.raise_if_encrypted(output_field, label)
        elif encoder is not None and value is not None:
            term = ValueWrapper(encoder(value))
            if expression_context.value_wrapper_refs is not None:
                expression_context.value_wrapper_refs.append(
                    (ValueRefOrigin.ANNOTATION, LiteralValueRef(term, encoder))
                )
        else:
            term = cast("Term", Term.wrap_constant(value))
            if expression_context.value_wrapper_refs is not None and isinstance(term, ValueWrapper):
                # A None/list/tuple value takes a DIFFERENT wrap_constant() branch (NullValue/
                # Array/Tuple, not a plain ValueWrapper) - not recorded; its description is None
                # (Expression.get_argument_plan_description()), so the query keeps no plan.
                expression_context.value_wrapper_refs.append((ValueRefOrigin.ANNOTATION, LiteralValueRef(term)))
        if isinstance(term, ValueWrapper):
            term = Value.get_typed_term(term, term.value, ParameterPosition.CASE_BRANCH, dialect)
        term = dialect.get_decimal_value_term(term)
        return ExpressionResult(term=term, joins=joins, output_field=output_field)

    def get_result(self, expression_context: ExpressionContext) -> ExpressionResult:
        return self.get_branch_result(
            expression_context, Value.get_literal_encoder(self.then, expression_context.dialect)
        )

    def get_branch_result(
        self, expression_context: ExpressionContext, then_encoder: Callable[[Any], Any] | None
    ) -> ExpressionResult:
        """Resolves this branch's condition and value.

        Args:
            expression_context: The context being resolved.
            then_encoder: Converts a literal ``then=`` into its bound form first, when given.

        Returns:
            The condition/value pair, the joins both need and the value's field.
        """
        q_objects = self._get_q_objects()

        # A CASE condition may read a window function - it is evaluated in SELECT/ORDER BY.
        condition_expression_context = dataclasses.replace(expression_context, window_function_filter_allowed=True)
        modifier = QueryModifier()
        for node in q_objects:
            modifier &= node.get_result(condition_expression_context)

        joins = list(modifier.joins)
        then_result = self.get_value_result(self.then, then_encoder, expression_context, "a When(then=...) branch")
        then = then_result.term
        joins.extend(then_result.joins)
        then_output_field = then_result.output_field  # type:ignore[call-overload]

        # Both criteria: a condition on an aggregate annotation lands in having_criterion, and a
        # CASE WHEN has one condition.
        condition = modifier._and_criterion()
        if not condition:
            # A When with no conditions is always true - 1=1, as SQL has no portable TRUE.
            condition = BasicCriterion(
                Equality.EQ,
                ValueWrapper(1, allow_parametrize=False),
                ValueWrapper(1, allow_parametrize=False),
            )
        return ExpressionResult(term=WhenThen(condition, then), joins=joins, output_field=then_output_field)
