from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any, ClassVar

from hare.dialects.enums import ParameterPosition
from hare.fields.data.json.json_field import JSONField
from hare.fields.generated_field import GeneratedField
from hare.gis.geometries.geometry import Geometry
from hare.query.expressions.arithmetic.combinable_expression import CombinableExpression
from hare.query.expressions.bound_expression import BoundExpression
from hare.query.expressions.case.when import When
from hare.query.expressions.compatibility.output_field_compatibility import OutputFieldCompatibility
from hare.query.expressions.enums import TemporalType
from hare.query.expressions.expression_context import ExpressionContext
from hare.query.expressions.expression_result import ExpressionResult, TableCriterionTuple
from hare.query.expressions.temporal.temporal_arithmetic import TemporalArithmetic
from hare.query.expressions.value import Value
from hare.query.plans.description.declared_plan_parts import DeclaredPlanParts
from hare.query.plans.enums import PlanPartType
from hare.sql import Case as HareSqlCase
from hare.sql.functions.cast import Cast as SqlCast
from hare.sql.terms.term import Term

if TYPE_CHECKING:
    from hare.dialects.base.dialect import Dialect
    from hare.fields.field import Field


class Case(CombinableExpression):
    """``CASE WHEN ... THEN ... ELSE ... END``: the value of the first branch whose condition holds.

    Args:
        args: The branches, ``When(...)`` each.
        default: The expression, SQL term or Python literal used when no branch holds.
            Literal support follows the value field and dialect, including custom field types;
            None gives ``NULL``.
    """

    # The result is decoded by the branches' field - a JSONField branch gives a dict, not JSON text.
    populate_field_object = True

    plan_parts: ClassVar[DeclaredPlanParts] = (("args", PlanPartType.EXPRESSIONS), ("default", PlanPartType.ARGUMENT))

    def __init__(
        self,
        *args: When,
        default: Any = None,
    ) -> None:
        self.args = args
        # A geometry is written as Value() writes it - a driver binds none.
        self.default = Value(default) if isinstance(default, Geometry) else default

    def get_result_literals(self) -> list[Any]:
        branch_values = [when.then for when in self.args if isinstance(when, When)] + [self.default]
        return [literal for branch_value in branch_values for literal in self.get_literals_of(branch_value)]

    def _get_branch_encoder(self, value: Any, dialect: Dialect) -> Callable[[Any], Any] | None:
        """The conversion a literal branch needs before it is bound.

        Args:
            value: The branch's ``then=``/``default=`` value.
            dialect: The dialect of the database the query runs on.

        Returns:
            For a date literal among datetime literals a datetime at midnight; for a UUID literal its
            field's stored form; otherwise the literal type's conversion.
        """
        literal = value.value if isinstance(value, Value) else value
        if isinstance(literal, date) and not isinstance(literal, datetime):
            branch_values = [when.then for when in self.args if isinstance(when, When)] + [self.default]
            if any(
                isinstance(branch_value.value if isinstance(branch_value, Value) else branch_value, datetime)
                for branch_value in branch_values
            ):
                return dialect.types.get_field_encoder(TemporalArithmetic.DATETIME_OUTPUT_FIELD)
        return Value.get_literal_encoder(literal, dialect)

    @staticmethod
    def get_wall_clock_literal_term(
        term: Term, output_field: Field[Any] | None, expression_context: ExpressionContext
    ) -> Term:
        """A wall clock literal branch cast to the wall clock column's type - a bare one would be cast to a
        moment's by its Python type.

        Args:
            term: The branch's term.
            output_field: The result's field - a wall clock's.
            expression_context: The context being resolved.

        Returns:
            The cast term.
        """
        if output_field is None:
            return term
        return SqlCast(term, output_field.get_column_type(expression_context.dialect))

    @staticmethod
    def get_timedelta_literal_encoder(
        output_field: Field[Any] | None, dialect: Dialect
    ) -> Callable[[Any], Any] | None:
        """The conversion of a timedelta branch - as an interval beside one (a field that ``holds_intervals``),
        else as a ``TimeDeltaField`` writes it.

        Args:
            output_field: The result's field.
            dialect: The dialect of the database the query runs on.

        Returns:
            The encoder.
        """
        if TemporalArithmetic.get_field_temporal_type(output_field) is TemporalType.INTERVAL:
            return dialect.types.get_field_encoder(output_field)  # type: ignore[arg-type]
        return Value.get_literal_encoder(timedelta(0), dialect)

    @staticmethod
    def get_naive_datetime_literal_encoder(
        output_field: Field[Any] | None, dialect: Dialect
    ) -> Callable[[Any], Any] | None:
        """The conversion of a naive datetime branch - as a wall clock beside one (``NaiveDatetimeField``),
        else as a moment of the configured zone.

        Args:
            output_field: The result's field.
            dialect: The dialect of the database the query runs on.

        Returns:
            The encoder.
        """
        if output_field is not None and TemporalArithmetic.is_naive_field(output_field):
            return dialect.types.get_field_encoder(output_field)
        return Value.get_literal_encoder(datetime.min, dialect)

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
        return dialect.types.get_field_encoder(output_field)

    def get_result(self, expression_context: ExpressionContext) -> ExpressionResult:
        """Select the branch type before encoding literals; bind every condition exactly once."""
        conditions: list[ExpressionResult] = []
        values: list[BoundExpression] = []
        for branch in self.args:
            if not isinstance(branch, When):
                raise TypeError("expected When objects as args")
            conditions.append(branch.get_condition_result(expression_context))
            values.append(
                BoundExpression.prepare_argument(
                    branch,
                    "then",
                    branch.then,
                    expression_context,
                    label="a When(then=...) branch",
                )
            )
        default = BoundExpression.prepare_argument(
            self,
            "default",
            self.default,
            expression_context,
            label="a Case(default=...) branch",
        )
        branches = [value.branch for value in (*values, default)]
        OutputFieldCompatibility.raise_if_value_types_mix(branches, "Case()")
        output_field = OutputFieldCompatibility.get_common_output_field(branches)
        renderers = expression_context.dialect.renderers
        case = HareSqlCase()
        joins: list[TableCriterionTuple] = []
        for condition, value in zip(conditions, values, strict=True):
            result = self._lower_branch(value, output_field, expression_context)
            joins.extend((*condition.joins, *result.joins))
            case = case.when(condition.term, renderers.get_result_branch_term(result.term, output_field))
        default_result = self._lower_branch(default, output_field, expression_context)
        joins.extend(default_result.joins)
        case = case.else_(renderers.get_result_branch_term(default_result.term, output_field))
        return ExpressionResult(
            term=case,
            joins=ExpressionResult.dedup_joins(joins),
            output_field=output_field,
        )

    def _lower_branch(
        self,
        value: BoundExpression,
        output_field: Field[Any] | None,
        context: ExpressionContext,
    ) -> ExpressionResult:
        """Choose the literal conversion from the common branch type and lower it once."""
        literal = value.branch.value
        wall_clock = False
        if value.branch.is_literal and isinstance(literal, dict):
            encoder = self.get_dict_literal_encoder(output_field, context.dialect)
        elif value.branch.is_literal and isinstance(literal, datetime) and literal.tzinfo is None:
            encoder = self.get_naive_datetime_literal_encoder(output_field, context.dialect)
            wall_clock = output_field is not None and TemporalArithmetic.is_naive_field(output_field)
        elif value.branch.is_literal and isinstance(literal, timedelta):
            encoder = self.get_timedelta_literal_encoder(output_field, context.dialect)
        else:
            encoder = self._get_branch_encoder(value.value, context.dialect)
        result = value.lower(
            context,
            encoder=encoder,
            position=None if wall_clock else ParameterPosition.CASE_BRANCH,
        )
        if wall_clock:
            return ExpressionResult(
                term=self.get_wall_clock_literal_term(result.term, output_field, context),
                joins=result.joins,
                output_field=result.output_field,
            )
        return result
