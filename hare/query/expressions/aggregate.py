from __future__ import annotations

from collections.abc import Sequence
from typing import Any, ClassVar, cast

from hare.exceptions import FieldError, QueryError
from hare.fields.encrypted.encrypted_field_base import EncryptedFieldBase
from hare.fields.field import Field
from hare.query.expressions.aggregate_paths.aggregated_multi_valued_paths import AggregatedMultiValuedPaths
from hare.query.expressions.arithmetic.combined_expression import CombinedExpression
from hare.query.expressions.conditions.q import Q
from hare.query.expressions.conditions.query_modifier import QueryModifier
from hare.query.expressions.expression_context import ExpressionContext
from hare.query.expressions.expression_result import ExpressionResult
from hare.query.expressions.f import F
from hare.query.expressions.function import Function
from hare.query.expressions.ordering import Ordering
from hare.query.expressions.subqueries.exists import Exists
from hare.query.plans.description.declared_plan_parts import DeclaredPlanParts
from hare.query.plans.enums import PlanPartType
from hare.sql import Field as HareSqlField
from hare.sql.functions.distinct_option_function import DistinctOptionFunction
from hare.sql.terms.arithmetic_expression import ArithmeticExpression
from hare.sql.terms.functions import AggregateFunction
from hare.sql.terms.functions.analytic_function import AnalyticFunction
from hare.sql.terms.functions.function import Function as HareSqlFunction
from hare.sql.terms.functions.window_frame_analytic_function import WindowFrameAnalyticFunction


class Aggregate(Function):
    """
    Base for SQL Aggregates.

    Args:
        field: Field name
        default_values: Extra parameters to the function.
        distinct: Flag for aggregate with distinction
        _filter: The condition of the rows aggregated (``FILTER (WHERE ...)``), None for every row.
        order_by: The order the values are aggregated in, for an ordered aggregate.
    """

    database_function: type[AggregateFunction] = DistinctOptionFunction
    #: The result doesn't change when a row repeats (MAX/MIN/BOOL_AND/BOOL_OR), so another
    #: to-many JOIN of the query can't inflate it.
    ignores_repeated_rows = False
    #: The result depends on the order the rows are read in (ARRAY_AGG/STRING_AGG/JSONB_AGG), so
    #: ``order_by=`` is accepted.
    allows_order_by = False
    #: Whether ``Window(...)`` computes the aggregate itself over the window - its own function, its
    #: extra arguments and its ``_filter=`` - where it has no window function of
    #: ``hare.query.functions.window``.
    computed_over_window = False

    plan_parts: ClassVar[DeclaredPlanParts] = (
        ("distinct", PlanPartType.KEY),
        ("get_plan_options", PlanPartType.KEY_METHOD),
        # Its orderings are among the options.
        ("order_by", PlanPartType.NONE),
        ("field", PlanPartType.FIELD),
        ("filter", PlanPartType.EXPRESSION),
        ("default_values", PlanPartType.ARGUMENTS),
        ("field_object", PlanPartType.NONE),
    )

    @staticmethod
    def _cast_integer_result_to_float(
        result: ExpressionResult, float_output_field: Field[Any], expression_context: ExpressionContext
    ) -> ExpressionResult:
        """The result of an aggregate over integers as the float it is decoded as - the dialect
        casts it where the database computes it as a decimal, which would otherwise be a Decimal
        inside arithmetic or text. Any other result is returned as it is.

        Args:
            result: The aggregate's result.
            float_output_field: The output field the aggregate gives integers.
            expression_context: The query's resolve context.

        Returns:
            The result.
        """
        if result.output_field is not float_output_field:  # type: ignore[call-overload]
            return result
        float_term = expression_context.dialect.renderers.get_integer_aggregate_as_float(result.term)
        if float_term is result.term:
            return result
        return ExpressionResult(
            term=float_term,
            joins=result.joins,
            output_field=result.output_field,  # type: ignore[call-overload]
        )

    def __init__(
        self,
        field: str | F | CombinedExpression,
        *default_values: Any,
        distinct: bool = False,
        _filter: Q | Exists | None = None,
        order_by: str | F | Ordering | Sequence[str | F | Ordering] = (),
    ) -> None:
        super().__init__(field, *default_values)
        self.order_by = self.get_validated_order_by(order_by)
        self.distinct = distinct
        if isinstance(_filter, Exists):
            _filter = Q(_filter)
        elif _filter is not None and not isinstance(_filter, Q):
            raise TypeError(
                f"{type(self).__name__}(_filter=...) takes a Q or an Exists condition, got {type(_filter).__name__}"
            )
        self.filter = _filter

    def get_validated_order_by(
        self, order_by: str | F | Ordering | Sequence[str | F | Ordering]
    ) -> tuple[str | Ordering, ...]:
        """The ``order_by=`` orderings as ordering strings and ``Ordering`` objects.

        Args:
            order_by: A field name (``"-name"`` for descending), ``F()``, ``F().asc()``/``.desc()``,
                or a sequence of them.

        Returns:
            The orderings.

        Raises:
            QueryError: The aggregate doesn't take ``order_by=``, or an ordering isn't one of those.
        """
        orderings = (order_by,) if isinstance(order_by, (str, F, Ordering)) else tuple(order_by)
        if orderings and not self.allows_order_by:
            raise QueryError(
                f"{type(self).__name__}() doesn't take order_by= - its result doesn't depend on row order"
            )
        validated_orderings: list[str | Ordering] = []
        for ordering in orderings:
            if isinstance(ordering, F):
                validated_orderings.append(ordering.name)
            elif isinstance(ordering, (str, Ordering)):
                validated_orderings.append(ordering)
            else:
                raise QueryError(
                    f"{type(self).__name__}(order_by=...) takes field names, F() or F().asc()/.desc(), "
                    f"got {type(ordering).__name__}"
                )
        return tuple(validated_orderings)

    def get_plan_options(self) -> tuple[Any, ...]:
        return (self.order_by,)

    def get_analytic_function(self, function: AggregateFunction) -> AnalyticFunction:
        """The aggregate's SQL function as a window function - with its arguments and its ``FILTER``.

        Args:
            function: The SQL aggregate this aggregate builds.

        Returns:
            The window function, its window not set yet.
        """
        analytic_function = WindowFrameAnalyticFunction(function.name, *function.args)
        if function._include_filter:
            analytic_function = analytic_function.filter(*function._filters)
        return analytic_function

    @staticmethod
    def raise_if_filter_reads_aggregate(function_name: str, filter_modifier: QueryModifier) -> None:
        """Rejects an aggregate's ``_filter=`` condition that reads an aggregate annotation, as Django does.

        Args:
            function_name: The aggregate's name, for the message.
            filter_modifier: The resolved ``_filter=`` condition.

        Raises:
            FieldError: The condition reads an aggregate.
        """
        if filter_modifier.having_criterion:
            raise FieldError(
                f"Cannot compute {function_name}(...): its _filter= condition reads an aggregate annotation - "
                "an aggregate can't be filtered by another aggregate. Filter the grouped rows with .filter() "
                "after .annotate() instead."
            )

    def get_result(self, expression_context: ExpressionContext) -> ExpressionResult:
        """Resolves the aggregate, attributing every to-many relation its expression crosses to it.

        Args:
            expression_context: Carries the model and virtual SQL table this aggregate is resolved
                against.

        Returns:
            The resolved term, joins, and output field.
        """
        tracker = AggregatedMultiValuedPaths.get_from(expression_context)
        if tracker is None:
            return super().get_result(expression_context)
        with tracker.aggregate_scope(self.distinct or self.ignores_repeated_rows, aggregate_name=type(self).__name__):
            return super().get_result(expression_context)

    def _get_function_term(self, function_arg: ExpressionResult, default_terms: list[Any]) -> HareSqlFunction:
        """Builds the aggregate, with ``FILTER (WHERE ...)`` when ``_filter=`` is given.

        Args:
            function_arg: The resolved main argument, carrying the filter condition.
            default_terms: The default value terms.

        Returns:
            The SQL aggregate term.
        """
        term = super()._get_function_term(function_arg, default_terms)
        if function_arg.aggregate_orderings:
            term = cast("AggregateFunction", term).order_arguments(*function_arg.aggregate_orderings)
        if function_arg.aggregate_filter is None:
            return term
        return cast("AggregateFunction", term).filter(function_arg.aggregate_filter)

    def _get_function_field(  # type:ignore[override]
        self, field: ArithmeticExpression | HareSqlField | str, *default_values: Any
    ) -> DistinctOptionFunction:
        function = cast("DistinctOptionFunction", super()._get_function_field(field, *default_values))
        if self.distinct:
            function = function.distinct()
        return function

    def _wrap_argument(
        self, expression_context: ExpressionContext, function_arg: ExpressionResult
    ) -> ExpressionResult:
        function_arg = self._with_argument_orderings(expression_context, function_arg)
        if not self.filter:
            return function_arg
        modifier = QueryModifier()
        tracker = AggregatedMultiValuedPaths.get_from(expression_context)
        if tracker is None:
            modifier &= self.filter.get_result(expression_context)
        else:
            with tracker.aggregate_filter_scope():
                modifier &= self.filter.get_result(expression_context)
        self.raise_if_filter_reads_aggregate(type(self).__name__, modifier)
        # Applied as `FILTER (WHERE ...)` (PostgreSQL, SQLite 3.30+) - rows failing it are left
        # out of the aggregate, not fed to it as NULL (ARRAY_AGG/JSONB_AGG would collect those).
        function_arg.aggregate_filter = modifier.where_criterion
        # The joins the filter's relation path needs are appended after the aggregate's own - a
        # deeper join depends on them, and joins are applied in list order.
        function_arg.joins = function_arg.joins + [join for join in modifier.joins if join not in function_arg.joins]

        return function_arg

    def _with_argument_orderings(
        self, expression_context: ExpressionContext, function_arg: ExpressionResult
    ) -> ExpressionResult:
        """Resolves ``order_by=`` into the argument's orderings, and their joins after its own.

        Args:
            expression_context: The context the aggregate is resolved in.
            function_arg: The resolved main argument.

        Returns:
            The argument.
        """
        if not self.order_by:
            return function_arg
        # Deferred import: hare.query.queryset imports this package at import time.
        from hare.query.queryset import QuerySet

        for ordering in self.order_by:
            field_name, order = QuerySet._get_ordering_string(ordering)
            field_result = F(field_name).get_result(expression_context)
            EncryptedFieldBase.raise_if_encrypted(field_result.output_field, f"{type(self).__name__}(order_by=...)")  # type: ignore[call-overload]
            function_arg.aggregate_orderings.append((field_result.term, order))
            function_arg.joins = function_arg.joins + [
                join for join in field_result.joins if join not in function_arg.joins
            ]
        return function_arg
