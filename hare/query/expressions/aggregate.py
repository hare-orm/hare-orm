from __future__ import annotations

from collections.abc import Sequence
from copy import copy
from typing import TYPE_CHECKING, Any, ClassVar, cast

from hare.classes.value_descriptions import ValueDescriptions
from hare.exceptions import FieldError, QueryError, UnSupportedError
from hare.fields.data.json.json_field import JSONField
from hare.fields.encrypted.encrypted_field_base import EncryptedFieldBase
from hare.fields.field import Field
from hare.fields.generated_field import GeneratedField
from hare.query.expressions.aggregate_paths.aggregated_multi_valued_paths import AggregatedMultiValuedPaths
from hare.query.expressions.arithmetic.combined_expression import CombinedExpression
from hare.query.expressions.conditions.q import Q
from hare.query.expressions.conditions.query_modifier import QueryModifier
from hare.query.expressions.expression import Expression
from hare.query.expressions.expression_context import ExpressionContext
from hare.query.expressions.expression_result import ExpressionResult
from hare.query.expressions.f import F
from hare.query.expressions.function import Function
from hare.query.expressions.numeric.numeric_typing import NumericTyping
from hare.query.expressions.ordering import Ordering
from hare.query.expressions.subqueries.exists import Exists
from hare.query.key_columns import KeyColumns
from hare.query.plans.description.declared_plan_parts import DeclaredPlanParts
from hare.query.plans.enums import PlanPartType
from hare.sql import Field as HareSqlField
from hare.sql.functions.distinct_option_function import DistinctOptionFunction
from hare.sql.terms.arithmetic_expression import ArithmeticExpression
from hare.sql.terms.criteria.criterion import Criterion
from hare.sql.terms.functions import AggregateFunction
from hare.sql.terms.functions.analytic_function import AnalyticFunction
from hare.sql.terms.functions.function import Function as HareSqlFunction
from hare.sql.terms.functions.window_frame_analytic_function import WindowFrameAnalyticFunction

if TYPE_CHECKING:
    from hare.dialects.base.dialect import Dialect


class Aggregate(Function):
    """The base of the aggregate functions: one value over the rows of a group.

    Args:
        field: The aggregated value - a field name or an expression.
        default_values: The function's further arguments.
        distinct: Whether each distinct value is aggregated once.
        _filter: The condition of the rows aggregated (``FILTER (WHERE ...)``), None for every row.
        order_by: The order the values are aggregated in, for an ordered aggregate.
    """

    database_function: type[AggregateFunction] = DistinctOptionFunction
    #: The result doesn't change when a row repeats (MAX/MIN/BOOL_AND/BOOL_OR), so another
    #: to-many JOIN of the query can't inflate it.
    ignores_repeated_rows = False
    #: The result depends on the order the rows are read in (an array, a text or a JSON array of them), so
    #: ``order_by=`` is accepted.
    allows_order_by = False
    #: Whether ``Window(...)`` computes the aggregate itself over the window - its own function, its
    #: extra arguments and its ``_filter=`` - where it has no window function of
    #: ``hare.query.functions.window``.
    computed_over_window = False
    #: The function the aggregate is computed by where the database orders its values by a key of
    #: them (``TypeMapping.ordering_key``) - ``ARG_MAX(value, key)`` for MAX; None for an aggregate
    #: reading no order.
    keyed_function_name: str | None = None

    plan_parts: ClassVar[DeclaredPlanParts] = (
        ("distinct", PlanPartType.KEY),
        ("get_plan_options", PlanPartType.KEY_METHOD),
        # Its orderings are among the options.
        ("order_by", PlanPartType.NONE),
        # The expressions it orders by, by their own descriptions - the options hold their places.
        ("order_by_expressions", PlanPartType.EXPRESSIONS),
        ("field", PlanPartType.FIELD),
        ("filter", PlanPartType.EXPRESSION),
        ("default_values", PlanPartType.ARGUMENTS),
        ("field_object", PlanPartType.NONE),
    )

    @staticmethod
    def _get_statistic_result(
        result: ExpressionResult, value_field: Field[Any] | None, expression_context: ExpressionContext
    ) -> ExpressionResult:
        """The result of an average or a statistic as the database computes it - over integers or
        Decimals the exact Decimal where it computes one, elsewhere a float, cast by the dialect where
        the database gives another type. Any other result is returned as it is.

        Args:
            result: The aggregate's result.
            value_field: The field of the values it reads.
            expression_context: The query's resolve context.

        Returns:
            The result.
        """
        if not NumericTyping.is_statistic_a_float(value_field, expression_context.dialect):
            return result
        float_term = expression_context.dialect.renderers.get_statistic_as_float(result.term)
        if float_term is result.term:
            return result
        return ExpressionResult(
            term=float_term,
            joins=result.joins,
            output_field=result.output_field,
        )

    def __init__(
        self,
        field: str | F | CombinedExpression,
        *default_values: Any,
        distinct: bool = False,
        _filter: Q | Exists | None = None,
        order_by: str | Expression | Ordering | Sequence[str | Expression | Ordering] = (),
    ) -> None:
        super().__init__(field, *default_values)
        self.order_by = self.get_validated_order_by(order_by)
        #: The expressions ``order_by`` orders by, in their order.
        self.order_by_expressions = tuple(
            ordering.target
            for ordering in self.order_by
            if isinstance(ordering, Ordering) and not isinstance(ordering.target, str)
        )
        self.distinct = distinct
        if isinstance(_filter, Exists):
            _filter = Q(_filter)
        elif _filter is not None and not isinstance(_filter, Q):
            raise TypeError(
                f"{type(self).__name__}(_filter=...) takes a Q or an Exists condition, got {type(_filter).__name__}"
            )
        self.filter = _filter

    def get_validated_order_by(
        self, order_by: str | Expression | Ordering | Sequence[str | Expression | Ordering] | None
    ) -> tuple[str | Ordering, ...]:
        """The ``order_by=`` orderings as ordering strings and ``Ordering`` objects.

        Args:
            order_by: A field name (``"-name"`` for descending), ``F()``, an expression, an
                ``Ordering`` (``F().asc()``, ``Lower("name").desc()``), or a sequence of them.

        Returns:
            The orderings.

        Raises:
            QueryError: The aggregate doesn't take ``order_by=``, or an ordering isn't one of those.
        """
        if order_by is None:
            return ()
        if isinstance(order_by, (str, Expression, Ordering)):
            orderings: tuple[Any, ...] = (order_by,)
        elif isinstance(order_by, (list, tuple)):
            orderings = tuple(order_by)
        else:
            raise QueryError(
                f"{type(self).__name__}(order_by=...) takes orderings, got "
                f"{ValueDescriptions.get_shown_value(order_by)}"
            )
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
            elif isinstance(ordering, Expression):
                validated_orderings.append(Ordering(ordering))
            else:
                raise QueryError(
                    f"{type(self).__name__}(order_by=...) takes field names, expressions or their "
                    ".asc()/.desc(), "
                    f"got {type(ordering).__name__}"
                )
        return tuple(validated_orderings)

    def get_plan_options(self) -> tuple[Any, ...]:
        # An ordering by an expression is keyed by its direction - the expression is described apart.
        return (
            tuple(
                ordering if isinstance(ordering, str) or isinstance(ordering.target, str) else (None, ordering.order)
                for ordering in self.order_by
            ),
        )

    def get_analytic_function(self, function: AggregateFunction) -> AnalyticFunction:
        """The aggregate's SQL function as a window function - with its arguments and its ``FILTER``.

        Args:
            function: The SQL aggregate this aggregate builds.

        Returns:
            The window function, its window not set yet.
        """
        analytic_function = WindowFrameAnalyticFunction(function.name, *function.args)
        analytic_function.result_holds_null = function.result_holds_null
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
            result = super().get_result(expression_context)
        else:
            with tracker.aggregate_scope(
                self.distinct or self.ignores_repeated_rows, aggregate_name=type(self).__name__
            ):
                result = super().get_result(expression_context)
        if self.keyed_function_name is not None:
            result.term = self.get_keyed_term(result, expression_context)
        output_field = result.output_field
        if (
            isinstance(result.term, AggregateFunction)
            and isinstance(output_field, Field)
            and not expression_context.dialect.types.holds_null(output_field)
        ):
            result.term.result_holds_null = False
        return result

    def get_keyed_term(self, result: ExpressionResult, expression_context: ExpressionContext) -> Any:
        """The aggregate's term, computed by the order of the dialect's key of its values where the
        dialect orders them by one (``TypeMapping.ordering_key``).

        Args:
            result: The resolved aggregate.
            expression_context: The context it is resolved in.

        Returns:
            ``keyed_function_name(value, key)``, or the term itself.
        """
        term = result.term
        field = result.output_field
        types = expression_context.dialect.types
        if (
            not isinstance(field, Field)
            or not types.has_ordering_key(field)
            or not isinstance(term, HareSqlFunction)
            or len(term.args) != 1
        ):
            return term
        ordering_key = types.get_ordering_key(field, term.args[0])
        if ordering_key is term.args[0]:
            return term
        keyed_term = copy(term)
        keyed_term.name = cast("str", self.keyed_function_name)
        keyed_term.args = [term.args[0], ordering_key]
        return keyed_term

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

    def get_function_term(  # type:ignore[override]
        self, field: ArithmeticExpression | HareSqlField | str, *default_values: Any
    ) -> DistinctOptionFunction:
        function = cast("DistinctOptionFunction", super().get_function_term(field, *default_values))
        if self.distinct:
            function = function.distinct()
        return function

    def raise_if_values_not_ordered(self, dialect: Dialect, output_field: Field[Any]) -> None:
        """Refuses ``Min()``/``Max()`` of a value the database orders none of - before any SQL.

        Args:
            dialect: The dialect the query runs on.
            output_field: The field of the argument's value.

        Raises:
            UnSupportedError: The value's field isn't orderable, or the database has no ``MIN``/``MAX`` of
                its column type.
        """
        value_field = GeneratedField.get_effective_field(output_field)
        if not value_field.is_orderable or not dialect.has_min_max_of(value_field.get_column_type(dialect)):
            raise UnSupportedError(
                f"{type(self).__name__}() of a {type(value_field).__name__} - the {dialect.name} database has no "
                f"{self.function_name} of its values"
            )

    def _wrap_argument(
        self, expression_context: ExpressionContext, function_arg: ExpressionResult
    ) -> ExpressionResult:
        if (
            self.keyed_function_name is not None
            and isinstance(
                GeneratedField.get_effective_field(function_arg.output_field)
                if function_arg.output_field is not None
                else None,
                JSONField,
            )
            and not expression_context.dialect.features.orders_json_values_in_aggregates
        ):
            # An aggregate comparing values would compare the JSON text (Features.orders_json_values_in_aggregates).
            raise FieldError(
                f"{type(self).__name__}() of a JSON value would compare its text on "
                f"{expression_context.dialect.name} - convert it with Cast()"
            )
        if self.keyed_function_name is not None and function_arg.output_field is not None:
            self.raise_if_values_not_ordered(expression_context.dialect, function_arg.output_field)
        function_arg = self._with_argument_orderings(expression_context, function_arg)
        missing_row_guard = self.get_missing_row_guard(expression_context, function_arg)
        if not self.filter:
            if missing_row_guard is not None:
                function_arg.aggregate_filter = missing_row_guard
            return function_arg
        modifier = QueryModifier()
        tracker = AggregatedMultiValuedPaths.get_from(expression_context)
        if tracker is None:
            modifier &= self.filter.get_result(expression_context)
        else:
            with tracker.aggregate_filter_scope():
                modifier &= self.filter.get_result(expression_context)
        self.raise_if_filter_reads_aggregate(type(self).__name__, modifier)
        # Applied as `FILTER (WHERE ...)` - rows failing it are left
        # out of the aggregate, not fed to it as NULL (one collecting values would collect those).
        function_arg.aggregate_filter = modifier.where_criterion
        if missing_row_guard is not None:
            function_arg.aggregate_filter = (
                missing_row_guard & modifier.where_criterion if modifier.where_criterion else missing_row_guard
            )
        # The joins the filter's relation path needs are appended after the aggregate's own - a
        # deeper join depends on them, and joins are applied in list order.
        function_arg.joins = function_arg.joins + [join for join in modifier.joins if join not in function_arg.joins]

        return function_arg

    @staticmethod
    def get_missing_row_guard(
        expression_context: ExpressionContext, function_arg: ExpressionResult
    ) -> Criterion | None:
        """The condition leaving a joined row that wasn't found out of the aggregate - for a column
        of it that holds no NULL (``TypeRegistry.holds_null()``): a LEFT JOIN that found no row gives
        it the type's empty value (an empty array, the point (0, 0)), which the aggregate would
        otherwise count as the row's.

        Args:
            expression_context: The context the aggregate is resolved in.
            function_arg: The resolved argument.

        Returns:
            The joined row's key IS NOT NULL, or None for an argument that needs no guard.
        """
        term = function_arg.term
        field = function_arg.output_field
        if (
            not function_arg.joins
            or not isinstance(term, HareSqlField)
            or not isinstance(field, Field)
            or field.model is None
            or term.table is None
            or term.table is expression_context.table
            or not field.model._meta.has_primary_key
            or expression_context.dialect.types.holds_null(field)
        ):
            return None
        return term.table[KeyColumns.get_source_columns(field.model._meta)[0]].notnull()

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
            target, order = QuerySet._get_ordering_string(ordering)
            field_result = (F(target) if isinstance(target, str) else target).get_result(expression_context)
            EncryptedFieldBase.raise_if_encrypted(field_result.output_field, f"{type(self).__name__}(order_by=...)")
            function_arg.aggregate_orderings.append((field_result.term, order))
            function_arg.joins = function_arg.joins + [
                join for join in field_result.joins if join not in function_arg.joins
            ]
        return function_arg
