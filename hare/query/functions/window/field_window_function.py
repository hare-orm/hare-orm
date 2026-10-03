from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, ClassVar

from hare.dialects.enums import ParameterPosition
from hare.exceptions import QueryError
from hare.query.expressions import Aggregate, Expression, Q
from hare.query.expressions.base.value import Value
from hare.query.plans.description.plan_context import PlanContext
from hare.query.plans.description.plan_description import PlanDescription
from hare.sql.terms.base.value_wrapper import ValueWrapper
from hare.sql.terms.functions.analytic_function import AnalyticFunction

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.fields.base.field import Field
    from hare.query.expressions import ExpressionContext
    from hare.query.expressions.base.expression_result import TableCriterionTuple
from hare.query.functions.window.window_function import WindowFunction


class FieldWindowFunction(WindowFunction):
    """Base for window functions that aggregate/read a single model field - or an expression, and
    only the rows matching a condition, when built from an aggregate (``Window(Sum("salary"))``)."""

    #: The aggregate of ``hare.query.functions`` computing the same value over a whole group.
    aggregate_class: ClassVar[type[Aggregate] | None] = None

    def __init__(self, field: str | Expression, condition: Q | None = None) -> None:
        self.field = field
        self.condition = condition

    def get_plan_arguments(self, context: PlanContext) -> Iterable[PlanDescription | None]:
        """The field or expression read, then the condition the rows are filtered by."""
        return (
            self._get_field_plan_description(context),
            PlanDescription.ABSENT if self.condition is None else self.condition.get_plan_description(context),
        )

    @classmethod
    def from_aggregate(cls, aggregate: Aggregate) -> FieldWindowFunction:
        """The window function computing an aggregate over the window, like Django's
        ``Window(Sum("salary"))``.

        Args:
            aggregate: An aggregate of ``hare.query.functions``.

        Returns:
            The window function.

        Raises:
            QueryError: The aggregate has no window counterpart, is ``distinct=True``, takes
                extra arguments or reads a raw SQL term.
        """
        window_function_classes = [
            window_function_class
            for window_function_class in cls.get_all_subclasses()
            if window_function_class.aggregate_class is type(aggregate)
        ]
        if not window_function_classes:
            raise QueryError(
                f"{type(aggregate).__name__}() can't be computed over a window - use Sum/Avg/Max/Min/Count/"
                "StdDev/Variance, or a window function of hare.query.functions.window."
            )
        if aggregate.distinct:
            raise QueryError(
                f"{type(aggregate).__name__}(distinct=True) can't be computed over a window - SQL has no DISTINCT "
                "window aggregate."
            )
        if aggregate.default_values:
            raise QueryError(f"{type(aggregate).__name__}() takes no extra arguments when computed over a window.")
        if not isinstance(aggregate.field, (str, Expression)):
            raise QueryError(
                f"{type(aggregate).__name__}() computed over a window takes a field name or an expression "
                f"(F(), a function, ...), got {type(aggregate.field).__name__}."
            )
        window_function = window_function_classes[0](aggregate.field, aggregate.filter)
        window_function.copy_aggregate_options(aggregate)
        return window_function

    @classmethod
    def get_all_subclasses(cls) -> list[type[FieldWindowFunction]]:
        """Every subclass, at any depth."""
        subclasses = []
        for subclass in cls.__subclasses__():
            subclasses.append(subclass)
            subclasses.extend(subclass.get_all_subclasses())
        return subclasses

    def copy_aggregate_options(self, aggregate: Aggregate) -> None:
        """Takes over options of the aggregate this window function computes - none by default."""

    def build(
        self, expression_context: ExpressionContext
    ) -> tuple[AnalyticFunction, list[TableCriterionTuple], "Field[Any] | None"]:
        field_result, value_field = self._resolve_field(expression_context)
        output_field = self._coerce_output_field(value_field)
        term = field_result.term
        if isinstance(term, ValueWrapper):
            # A literal annotation read by the window has no type the database could infer.
            term = Value.get_typed_term(
                term, term.value, ParameterPosition.FUNCTION_ARGUMENT, expression_context.dialect
            )
        analytic_term = self.get_analytic_term(term)
        joins = field_result.joins
        if self.condition:
            # Only the window's rows matching the condition are aggregated - FILTER (WHERE ...).
            # An empty Q() (also negated or nested) keeps every row, as for a plain aggregate.
            condition_modifier = self.condition.get_result(expression_context)
            Aggregate.raise_if_filter_reads_aggregate(type(self).__name__, condition_modifier)
            analytic_term = analytic_term.filter(condition_modifier.where_criterion)
            joins = joins + [join for join in condition_modifier.joins if join not in joins]
        return analytic_term, joins, output_field

    def _coerce_output_field(self, field_object: "Field[Any] | None") -> "Field[Any] | None":
        """The field this function's result decodes through.

        Args:
            field_object: The aggregated/read field.

        Returns:
            The field itself - the result has the same type as the values it is computed from.
        """
        return field_object
