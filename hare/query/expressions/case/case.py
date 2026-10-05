from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from typing import TYPE_CHECKING, Any, ClassVar, cast

from hare.fields.data.json.json_field import JSONField
from hare.fields.generated_field import GeneratedField
from hare.query.expressions.arithmetic.combinable_expression import CombinableExpression
from hare.query.expressions.compatibility.output_field_compatibility import OutputFieldCompatibility
from hare.query.expressions.compatibility.result_branch import ResultBranch
from hare.query.expressions.expression_context import ExpressionContext
from hare.query.expressions.expression_result import ExpressionResult
from hare.query.expressions.temporal.temporal_arithmetic import TemporalArithmetic
from hare.query.expressions.value import Value
from hare.query.plans.description.declared_plan_parts import DeclaredPlanParts
from hare.query.plans.enums import PlanPartType
from hare.sql import Case as HareSqlCase

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.dialect import Dialect
    from hare.fields.field import Field
from hare.query.expressions.case.case_branch_value import CaseBranchValue
from hare.query.expressions.case.when import When
from hare.sql.terms.case.when_then import WhenThen


class Case(CombinableExpression):
    """
    Case expression.

    Args:
        args: When objects
        default: value for 'CASE WHEN ... THEN ... ELSE <default> END'
    """

    # The result is decoded by the branches' field - a JSONField branch gives a dict, not JSON text.
    populate_field_object = True

    plan_parts: ClassVar[DeclaredPlanParts] = (("args", PlanPartType.EXPRESSIONS), ("default", PlanPartType.ARGUMENT))

    def __init__(
        self,
        *args: When,
        default: CaseBranchValue = None,
    ) -> None:
        self.args = args
        self.default = default

    def _get_branch_encoder(self, value: Any, dialect: Dialect) -> Callable[[Any], Any] | None:
        """The conversion a literal branch needs before it is bound.

        Args:
            value: The branch's ``then=``/``default=`` value.
            dialect: The dialect of the database the query runs on.

        Returns:
            For a date literal among datetime literals a datetime at midnight; for a timedelta or
            UUID literal its field's stored form; a dict as it is - encoded once the result's field
            is known (``get_dict_literal_encoder()``); None otherwise.
        """
        literal = value.value if isinstance(value, Value) else value
        if isinstance(literal, dict):
            return Case.keep_value
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

    @staticmethod
    def keep_value(value: Any) -> Any:
        """A literal left as it is until the result's field encodes it."""
        return value

    @staticmethod
    def get_dict_literal_encoder(output_field: Field[Any] | None, dialect: Dialect) -> Callable[[Any], Any] | None:
        """The conversion of a dict literal branch - as the result's field writes it (a map), its JSON
        text for a JSON or an unknown result.

        Args:
            output_field: The result's field.
            dialect: The dialect of the database the query runs on.

        Returns:
            The encoder.
        """
        if output_field is None or isinstance(GeneratedField.get_effective_field(output_field), JSONField):
            return Value.get_literal_encoder({}, dialect)
        return TemporalArithmetic.get_field_encoder(output_field, dialect)

    def get_result(self, expression_context: ExpressionContext) -> ExpressionResult:
        branch_terms: list[tuple[Any, Any]] = []
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
            branch_terms.append((when_term.when, when_term.then))
            joins.extend(when.joins)
            branches.append(ResultBranch.of(arg.then, when.output_field))  # type:ignore[call-overload]

        default_result = When.get_value_result(
            self,
            "default",
            self.default,
            self._get_branch_encoder(self.default, expression_context.dialect),
            expression_context,
            "a Case(default=...) branch",
        )
        joins.extend(default_result.joins)
        branches.append(ResultBranch.of(self.default, default_result.output_field))  # type:ignore[call-overload]

        OutputFieldCompatibility.raise_if_value_types_mix(branches, "Case()")
        output_field = OutputFieldCompatibility.get_common_output_field(branches)
        dict_branch_positions = [
            position
            for position, arg in enumerate(self.args)
            if isinstance(arg.then.value if isinstance(arg.then, Value) else arg.then, dict)
        ]
        default_is_dict = isinstance(self.default.value if isinstance(self.default, Value) else self.default, dict)
        if dict_branch_positions or default_is_dict:
            # A dict literal is written as the result's field writes it - a map's, or JSON text.
            dict_encoder = self.get_dict_literal_encoder(output_field, expression_context.dialect)
            for position in dict_branch_positions:
                when_term = cast(
                    "WhenThen", self.args[position].get_branch_result(expression_context, dict_encoder).term
                )
                branch_terms[position] = (when_term.when, when_term.then)
            if default_is_dict:
                default_result = When.get_value_result(
                    self, "default", self.default, dict_encoder, expression_context, "a Case(default=...) branch"
                )
        renderers = expression_context.dialect.renderers
        case = HareSqlCase()
        for criterion, then_term in branch_terms:
            case = case.when(criterion, renderers.get_result_branch_term(then_term, output_field))
        case = case.else_(renderers.get_result_branch_term(default_result.term, output_field))
        return ExpressionResult(term=case, joins=ExpressionResult.dedup_joins(joins), output_field=output_field)
