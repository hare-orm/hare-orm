from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any, ClassVar, cast

from hare.fields.data.json.json_field import JSONField
from hare.fields.generated_field import GeneratedField
from hare.gis.geometries.geometry import Geometry
from hare.query.expressions.arithmetic.combinable_expression import CombinableExpression
from hare.query.expressions.compatibility.output_field_compatibility import OutputFieldCompatibility
from hare.query.expressions.compatibility.result_branch import ResultBranch
from hare.query.expressions.enums import TemporalType
from hare.query.expressions.expression_context import ExpressionContext
from hare.query.expressions.expression_result import ExpressionResult
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
from hare.query.expressions.case.case_branch_value import CaseBranchValue
from hare.query.expressions.case.when import When
from hare.sql.terms.case.when_then import WhenThen


class Case(CombinableExpression):
    """``CASE WHEN ... THEN ... ELSE ... END``: the value of the first branch whose condition holds.

    Args:
        args: The branches, ``When(...)`` each.
        default: The value when no branch holds - the ``ELSE``; None gives ``NULL``.
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
            field's stored form; a dict, a naive datetime and a timedelta as they are - encoded once the
            result's field is known; None otherwise.
        """
        literal = value.value if isinstance(value, Value) else value
        if isinstance(literal, (dict, timedelta)) or (isinstance(literal, datetime) and literal.tzinfo is None):
            return Case.keep_value
        if isinstance(literal, date) and not isinstance(literal, datetime):
            branch_values = [when.then for when in self.args if isinstance(when, When)] + [self.default]
            if any(
                isinstance(branch_value.value if isinstance(branch_value, Value) else branch_value, datetime)
                for branch_value in branch_values
            ):
                return TemporalArithmetic.get_field_encoder(
                    TemporalArithmetic.DATETIME_OUTPUT_FIELD,
                    dialect,
                )
        return Value.get_literal_encoder(literal, dialect)

    @staticmethod
    def keep_value(value: Any) -> Any:
        """A literal left as it is until the result's field encodes it."""
        return value

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
    def is_deferred_literal(value: Any, literal_type: type) -> bool:
        """Whether a branch is a literal encoded once the result's field is known - a dict, a naive
        datetime, a timedelta.

        Args:
            value: The branch's ``then=``/``default=`` value.
            literal_type: ``dict``, ``datetime`` or ``timedelta``.

        Returns:
            Whether it is such a literal.
        """
        literal = value.value if isinstance(value, Value) else value
        if literal_type is datetime:
            return isinstance(literal, datetime) and literal.tzinfo is None
        return isinstance(literal, literal_type)

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
            return TemporalArithmetic.get_field_encoder(output_field, dialect)  # type: ignore[arg-type]
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
            return TemporalArithmetic.get_field_encoder(output_field, dialect)
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
            branches.append(ResultBranch.of(arg.then, when.output_field))

        default_result = When.get_value_result(
            self,
            "default",
            self.default,
            self._get_branch_encoder(self.default, expression_context.dialect),
            expression_context,
            "a Case(default=...) branch",
        )
        joins.extend(default_result.joins)
        branches.append(ResultBranch.of(self.default, default_result.output_field))

        OutputFieldCompatibility.raise_if_value_types_mix(branches, "Case()")
        output_field = OutputFieldCompatibility.get_common_output_field(branches)
        is_wall_clock = output_field is not None and TemporalArithmetic.is_naive_field(output_field)
        for literal_type, deferred_encoder in (
            (dict, self.get_dict_literal_encoder(output_field, expression_context.dialect)),
            (datetime, self.get_naive_datetime_literal_encoder(output_field, expression_context.dialect)),
            (timedelta, self.get_timedelta_literal_encoder(output_field, expression_context.dialect)),
        ):
            # A wall clock literal is typed by the wall clocks beside it - its own type is a moment's.
            types_literal = not (literal_type is datetime and is_wall_clock)
            # A dict literal is written as the result's field writes it - a map's, or JSON text; a naive
            # datetime as a wall clock beside one, else as a moment of the configured zone.
            positions = [
                position for position, arg in enumerate(self.args) if self.is_deferred_literal(arg.then, literal_type)
            ]
            for position in positions:
                when_term = cast(
                    "WhenThen",
                    self.args[position].get_branch_result(expression_context, deferred_encoder, types_literal).term,
                )
                branch_terms[position] = (
                    when_term.when,
                    self.get_wall_clock_literal_term(when_term.then, output_field, expression_context)
                    if not types_literal
                    else when_term.then,
                )
            if self.is_deferred_literal(self.default, literal_type):
                default_result = When.get_value_result(
                    self,
                    "default",
                    self.default,
                    deferred_encoder,
                    expression_context,
                    "a Case(default=...) branch",
                    types_literal,
                )
                if not types_literal:
                    default_result.term = self.get_wall_clock_literal_term(
                        default_result.term, output_field, expression_context
                    )
        renderers = expression_context.dialect.renderers
        case = HareSqlCase()
        for criterion, then_term in branch_terms:
            case = case.when(criterion, renderers.get_result_branch_term(then_term, output_field))
        case = case.else_(renderers.get_result_branch_term(default_result.term, output_field))
        return ExpressionResult(
            term=case,
            joins=ExpressionResult.dedup_joins(joins),
            output_field=output_field,
        )
