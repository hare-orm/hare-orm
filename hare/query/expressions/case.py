from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from typing import TYPE_CHECKING, Any, cast

from hare.query.expressions.base.arithmetic_expression_mixin import ArithmeticExpressionMixin
from hare.query.expressions.base.expression_context import ExpressionContext
from hare.query.expressions.base.expression_result import ExpressionResult
from hare.query.expressions.base.value import Value
from hare.query.expressions.compatibility.output_field_compatibility import OutputFieldCompatibility
from hare.query.expressions.compatibility.result_branch import ResultBranch
from hare.query.expressions.temporal.temporal_arithmetic import TemporalArithmetic
from hare.query.plans.description.plan_context import PlanContext
from hare.query.plans.description.plan_description import PlanDescription
from hare.sql import Case as HareSqlCase

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.dialect import Dialect
from hare.query.expressions.case_branch_value import CaseBranchValue
from hare.query.expressions.when import When
from hare.query.expressions.when_then import WhenThen


class Case(ArithmeticExpressionMixin):
    """
    Case expression.

    Args:
        args: When objects
        default: value for 'CASE WHEN ... THEN ... ELSE <default> END'
    """

    # The result is decoded by the branches' field - a JSONField branch gives a dict, not JSON text.
    populate_field_object = True

    def __init__(
        self,
        *args: When,
        default: CaseBranchValue = None,
    ) -> None:
        self.args = args
        self.default = default

    def get_plan_description(self, context: PlanContext) -> PlanDescription | None:
        """Each branch, then the default.

        Args:
            context: The context the expression is resolved in.

        Returns:
            The description, None when a branch or the default keeps no plan.
        """
        return PlanDescription.combine(
            Case,
            (
                *(when.get_plan_description(context) for when in self.args),
                self.get_argument_plan_description(self.default, context),
            ),
        )

    def _get_branch_encoder(self, value: Any, dialect: Dialect) -> Callable[[Any], Any] | None:
        """The conversion a literal branch needs before it is bound.

        Args:
            value: The branch's ``then=``/``default=`` value.
            dialect: The dialect of the database the query runs on.

        Returns:
            For a date literal among datetime literals a datetime at midnight; for a timedelta,
            UUID or dict literal its field's stored form; None otherwise.
        """
        literal = value.value if isinstance(value, Value) else value
        if isinstance(literal, date) and not isinstance(literal, datetime):
            branch_values = [when.then for when in self.args if isinstance(when, When)] + [self.default]
            if any(
                isinstance(branch_value.value if isinstance(branch_value, Value) else branch_value, datetime)
                for branch_value in branch_values
            ):
                return TemporalArithmetic.get_field_encoder(
                    TemporalArithmetic.DATETIME_OUTPUT_FIELD,  # type: ignore[arg-type]
                    dialect,
                )
        return Value.get_literal_encoder(literal, dialect)

    def get_result(self, expression_context: ExpressionContext) -> ExpressionResult:
        case = HareSqlCase()
        joins = []
        # The result's field comes from every branch together (see OutputFieldCompatibility.
        # get_common_output_field()): numbers of different types widen to a float or a Decimal
        # with the largest scale, any other field is kept only when every branch fits it.
        branches: list[ResultBranch] = []
        for arg in self.args:
            if not isinstance(arg, When):
                raise TypeError("expected When objects as args")
            when = arg.get_branch_result(
                expression_context, self._get_branch_encoder(arg.then, expression_context.dialect)
            )
            when_term = cast("WhenThen", when.term)
            case = case.when(when_term.when, when_term.then)
            joins.extend(when.joins)
            branches.append(ResultBranch.of(arg.then, when.output_field))  # type:ignore[call-overload]

        default_result = When.get_value_result(
            self.default,
            self._get_branch_encoder(self.default, expression_context.dialect),
            expression_context,
            "a Case(default=...) branch",
        )
        case = case.else_(default_result.term)
        joins.extend(default_result.joins)
        branches.append(ResultBranch.of(self.default, default_result.output_field))  # type:ignore[call-overload]

        output_field = OutputFieldCompatibility.get_common_output_field(branches)
        return ExpressionResult(term=case, joins=ExpressionResult.dedup_joins(joins), output_field=output_field)
