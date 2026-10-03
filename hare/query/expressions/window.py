from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from hare.exceptions import QueryError
from hare.fields.encrypted.encrypted_field_mixin import EncryptedFieldMixin
from hare.query.expressions.aggregate import Aggregate
from hare.query.expressions.base.expression import Expression
from hare.query.expressions.base.expression_context import ExpressionContext
from hare.query.expressions.base.expression_result import ExpressionResult
from hare.query.expressions.f import F
from hare.query.expressions.ordering import Ordering
from hare.query.plans.description.plan_context import PlanContext
from hare.query.plans.description.plan_description import PlanDescription
from hare.sql.analytics.declarations import Following, Preceding
from hare.sql.terms.functions.window_frame_analytic_function import WindowFrameAnalyticFunction

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.query.functions.window.window_function import WindowFunction


class Window(Expression):
    """Window function expression, e.g. ``Window(RowNumber(), partition_by=["category"],
    order_by=["-created_at"])``. Use via ``.annotate(...)``.

    Args:
        expression: A window function from ``hare.query.functions.window`` (``RowNumber``, ``Rank``,
            ``Sum``, ``Lag``, ...), or an aggregate from ``hare.query.functions`` (``Sum``, ``Avg``,
            ``Max``, ``Min``, ``Count``, ``StdDev``, ``Variance`` - its ``_filter=`` included), computed over
            the window.
        partition_by: Field names to partition the window by.
        order_by: Field names to order rows within each partition by (``-field`` for descending),
            or ``F("field").asc()``/``.desc()`` orderings to also fix the position of NULLs.
    """

    # The result is decoded by the aggregated field. A ranking function has none and stays an int.
    populate_field_object = True

    def __init__(
        self,
        expression: WindowFunction | Aggregate,
        partition_by: Sequence[str] = (),
        order_by: Sequence[str | Ordering] = (),
    ) -> None:
        if isinstance(expression, Aggregate):
            # Deferred import: hare.query.functions.window imports this package at import time.
            from hare.query.functions.window.field_window_function import FieldWindowFunction

            expression = FieldWindowFunction.from_aggregate(expression)
        self.expression = expression
        self.partition_by = partition_by
        self.order_by = order_by

    def get_plan_description(self, context: PlanContext) -> PlanDescription | None:
        """The window function's description, then the partition and the ordering - a name among
        them naming an annotation resolves it again.

        Args:
            context: The context the window is resolved in.

        Returns:
            The description, None when the window function keeps no plan.
        """
        # Deferred import: hare.query.queryset imports this package at import time.
        from hare.query.queryset import QuerySet

        reference_values = [
            *(
                value
                for field_name in self.partition_by
                for value in self.get_referenced_annotation_values(field_name, context)
            ),
            *(
                value
                for ordering in self.order_by
                for value in self.get_referenced_annotation_values(QuerySet._get_ordering_string(ordering)[0], context)
            ),
        ]
        return PlanDescription.combine(
            (Window, tuple(self.partition_by), tuple(self.order_by)),
            (self.expression.get_plan_description(context), PlanDescription((), reference_values)),
        )

    def _reject_nested_window_reference(self, expression_context: ExpressionContext, field_name: str) -> None:
        """Raises when a ``partition_by``/``order_by`` name is another ``Window(...)`` annotation - a
        window function isn't allowed in a window definition.
        """
        annotation = expression_context.annotations.get(field_name)
        if isinstance(annotation, Window):
            raise QueryError(
                f"partition_by/order_by can't reference '{field_name}' - it's itself a Window(...) "
                "annotation, and nesting one window function inside another window function's own "
                "window definition isn't valid SQL. Partition/order by the same underlying field(s) "
                "directly instead."
            )

    def get_result(self, expression_context: ExpressionContext) -> ExpressionResult:
        # Deferred import: hare.query.queryset imports Q/Expression/... from this package at
        # import time, so a direct top-level import here would be circular (same reason as
        # elsewhere in this package).
        from hare.query.queryset import QuerySet

        term, joins, output_field = self.expression.build(expression_context)
        if self.expression.full_partition_frame and isinstance(term, WindowFrameAnalyticFunction):
            # e.g. LastValue - see WindowFunction.full_partition_frame's own docstring for why
            # SQL's own default frame doesn't work for it.
            term = term.rows(Preceding(), Following())

        partition_terms = []
        for field_name in self.partition_by:
            self._reject_nested_window_reference(expression_context, field_name)
            field_result = F(field_name).get_result(expression_context)
            EncryptedFieldMixin.raise_if_encrypted(field_result.output_field, "Window(partition_by=...)")  # type:ignore[call-overload]
            partition_terms.append(field_result.term)
            joins = joins + field_result.joins

        term = term.over(*partition_terms)
        for ordering in self.order_by:
            field_name, order = QuerySet._get_ordering_string(ordering)
            self._reject_nested_window_reference(expression_context, field_name)
            field_result = F(field_name).get_result(expression_context)
            EncryptedFieldMixin.raise_if_encrypted(field_result.output_field, "Window(order_by=...)")  # type:ignore[call-overload]
            term = term.orderby(field_result.term, order=order)
            joins = joins + field_result.joins

        return ExpressionResult(term=term, joins=joins, output_field=output_field)
