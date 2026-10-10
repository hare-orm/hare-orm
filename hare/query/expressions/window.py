from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, ClassVar

from hare.exceptions import QueryError
from hare.fields.encrypted.encrypted_field_base import EncryptedFieldBase
from hare.query.expressions.aggregate import Aggregate
from hare.query.expressions.expression import Expression
from hare.query.expressions.expression_context import ExpressionContext
from hare.query.expressions.expression_result import ExpressionResult
from hare.query.expressions.f import F
from hare.query.expressions.frames.window_frame import WindowFrame
from hare.query.expressions.ordering import Ordering
from hare.query.plans.description.declared_plan_parts import DeclaredPlanParts
from hare.query.plans.enums import PlanPartType
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
        frame: The rows around the current one the function computes over - ``RowRange(start=-2,
            end=0)``, ``ValueRange(start=-10, end=0)``; the database's default frame without it. An
            aggregate or a value function (``FirstValue``, ``LastValue``, ``NthValue``) takes one;
            a ranking function (``RowNumber``, ``Rank``, ...) doesn't.

    Raises:
        QueryError: ``frame`` isn't a ``WindowFrame``.
    """

    # The result is decoded by the aggregated field. A ranking function has none and stays an int.
    populate_field_object = True

    plan_parts: ClassVar[DeclaredPlanParts] = (
        ("expression", PlanPartType.EXPRESSION),
        ("partition_by", PlanPartType.KEYS),
        ("order_by", PlanPartType.KEYS),
        ("frame", PlanPartType.PLAN_KEY),
    )

    def __init__(
        self,
        expression: WindowFunction | Aggregate,
        partition_by: Sequence[str] = (),
        order_by: Sequence[str | Ordering] = (),
        frame: WindowFrame | None = None,
    ) -> None:
        if frame is not None and not isinstance(frame, WindowFrame):
            raise QueryError(f"Window(frame=...) takes a RowRange or a ValueRange, got {frame!r}")
        self.frame = frame
        if isinstance(expression, Aggregate):
            # Deferred import: hare.query.functions.window imports this package at import time.
            from hare.query.functions.window.field_window_function import FieldWindowFunction

            expression = FieldWindowFunction.from_aggregate(expression)
        self.expression = expression
        self.partition_by = partition_by
        self.order_by = order_by

    @staticmethod
    def _reject_nested_window_reference(expression_context: ExpressionContext, field_name: str) -> None:
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
        if self.frame is not None:
            if not isinstance(term, WindowFrameAnalyticFunction):
                raise QueryError(
                    "Window(frame=...) takes an aggregate or a value function, not "
                    f"{type(self.expression).__name__} - it computes over the whole partition"
                )
            if self.frame.offsets_need_one_ordering and self.frame.has_offsets() and len(self.order_by) != 1:
                raise QueryError(
                    f"{type(self.frame).__name__} with an offset measures the ordering's value - order the window "
                    f"by exactly one field, got order_by={list(self.order_by)!r}"
                )
            term = self.frame.apply(term)
        elif self.expression.full_partition_frame and isinstance(term, WindowFrameAnalyticFunction):
            # e.g. LastValue - see WindowFunction.full_partition_frame's own docstring for why
            # SQL's own default frame doesn't work for it.
            term = term.rows(Preceding(), Following())

        partition_terms = []
        for field_name in self.partition_by:
            self._reject_nested_window_reference(expression_context, field_name)
            field_result = F(field_name).get_result(expression_context)
            EncryptedFieldBase.raise_if_encrypted(field_result.output_field, "Window(partition_by=...)")  # type:ignore[call-overload]
            partition_terms.append(field_result.term)
            joins = joins + field_result.joins

        term = term.over(*partition_terms)
        for ordering in self.order_by:
            field_name, order = QuerySet._get_ordering_string(ordering)
            self._reject_nested_window_reference(expression_context, field_name)
            field_result = F(field_name).get_result(expression_context)
            EncryptedFieldBase.raise_if_encrypted(field_result.output_field, "Window(order_by=...)")  # type:ignore[call-overload]
            term = term.orderby(field_result.term, order=order)
            joins = joins + field_result.joins

        return ExpressionResult(term=term, joins=joins, output_field=output_field)
