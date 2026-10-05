from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, ClassVar

from hare.exceptions import QueryError
from hare.fields.encrypted.encrypted_field_base import EncryptedFieldBase
from hare.query.expressions.aggregate import Aggregate
from hare.query.expressions.arithmetic.combinable_expression import CombinableExpression
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

if TYPE_CHECKING:
    from hare.query.functions.window.window_function import WindowFunction


class Window(CombinableExpression):
    """Window function expression, e.g. ``Window(RowNumber(), partition_by=["category"],
    order_by=["-created_at"])``. Use via ``.annotate(...)``.

    Args:
        expression: A window function from ``hare.query.functions.window`` (``RowNumber``, ``Rank``,
            ``Sum``, ``Lag``, ...), or an aggregate from ``hare.query.functions`` (``Sum``, ``Avg``,
            ``Max``, ``Min``, ``Count``, ``StdDev``, ``Variance`` - its ``_filter=`` included), computed over
            the window.
        partition_by: Field names or expressions to partition the window by.
        order_by: Field names to order rows within each partition by (``-field`` for descending),
            expressions, or ``F("field").asc()``/``Lower("name").desc()`` orderings to also fix the
            position of NULLs.
        frame: The rows around the current one the function computes over - ``RowRange(start=-2,
            end=0)``, ``ValueRange(start=-10, end=0)``; the database's default frame without it. An
            aggregate or a value function (``FirstValue``, ``LastValue``, ``NthValue``) takes one;
            a ranking function (``RowNumber``, ``Rank``, ...) doesn't.

    Raises:
        QueryError: ``partition_by`` or ``order_by`` isn't a list, an item of them isn't a name, an
            expression or (``order_by``) an ordering, or ``frame`` isn't a ``WindowFrame``.
    """

    # The result is decoded by the aggregated field. A ranking function has none and stays an int.
    populate_field_object = True

    plan_parts: ClassVar[DeclaredPlanParts] = (
        ("expression", PlanPartType.EXPRESSION),
        # The names and directions - an expression keyed by its place, described apart.
        ("partition_by", PlanPartType.NONE),
        ("order_by", PlanPartType.NONE),
        ("get_window_key", PlanPartType.KEY_METHOD),
        ("window_expressions", PlanPartType.EXPRESSIONS),
        ("frame", PlanPartType.PLAN_KEY),
    )

    def __init__(
        self,
        expression: WindowFunction | Aggregate,
        partition_by: Sequence[str | Expression] = (),
        order_by: Sequence[str | Expression | Ordering] = (),
        frame: WindowFrame | None = None,
    ) -> None:
        partition_by = partition_by or ()
        order_by = order_by or ()
        self.frame = frame
        for item in partition_by:
            Window.raise_if_not_window_item("partition_by", item)
        for item in order_by:
            Window.raise_if_not_window_item("order_by", item.target if isinstance(item, Ordering) else item)
        if isinstance(expression, Aggregate):
            # Deferred import: hare.query.functions.window imports this package at import time.
            from hare.query.functions.window.field_window_function import FieldWindowFunction

            expression = FieldWindowFunction.from_aggregate(expression)
        self.expression = expression
        self.partition_by = tuple(item.name if isinstance(item, F) else item for item in partition_by)
        self.order_by = tuple(
            item.name if isinstance(item, F) else Ordering(item) if isinstance(item, Expression) else item
            for item in order_by
        )
        #: The expressions it partitions and orders by, in their order.
        self.window_expressions = (
            *(item for item in self.partition_by if not isinstance(item, str)),
            *(
                item.target
                for item in self.order_by
                if isinstance(item, Ordering) and not isinstance(item.target, str)
            ),
        )

    @staticmethod
    def raise_if_not_window_item(argument_name: str, item: Any) -> None:
        """Rejects a ``partition_by``/``order_by`` item that isn't a field or annotation name or an
        expression - any other value would be written into the SQL as a constant to partition or order by.

        Args:
            argument_name: ``partition_by`` or ``order_by``, for the message.
            item: The item, an ordering's target for ``order_by``.

        Raises:
            QueryError: It isn't one.
        """
        if not isinstance(item, str | Expression):
            raise QueryError(
                f"Window({argument_name}=...) takes field names and expressions, got {type(item).__name__}"
            )

    @staticmethod
    def get_read_targets(
        expression_context: ExpressionContext, target: str | Expression
    ) -> tuple[str | Expression, ...]:
        """What a window partitions or orders by for one of its items - a primary key or a relation of
        several columns its columns, as ``order_by()`` reads them.

        Args:
            expression_context: The query's resolve context.
            target: A field path, an annotation name or an expression.

        Returns:
            The field paths, or the item itself.
        """
        if not isinstance(target, str) or target in expression_context.annotations:
            return (target,)
        # Deferred import: the queryset package imports this one at import time.
        from hare.query.queryset.concrete_field_paths import ConcreteFieldPaths

        return ConcreteFieldPaths.get_paths(expression_context.model, target)

    def get_window_key(self) -> tuple[Any, ...]:
        """What the window's partitions and ordering write into the SQL text - an expression by its
        place, its own description beside.

        Returns:
            The partition names and the orderings.
        """
        return (
            tuple(item if isinstance(item, str) else None for item in self.partition_by),
            tuple(
                item if isinstance(item, str) or isinstance(item.target, str) else (None, item.order)
                for item in self.order_by
            ),
        )

    @staticmethod
    def _reject_nested_window_reference(expression_context: ExpressionContext, field_name: str) -> None:
        """Raises when a ``partition_by``/``order_by`` name is another ``Window(...)`` annotation - a
        window function isn't allowed in a window definition.
        """
        annotation = expression_context.annotations.get(field_name)
        if isinstance(annotation, Window):
            raise QueryError(
                f"partition_by/order_by can't reference {field_name!r} - it's itself a Window(...) "
                "annotation, and nesting one window function inside another window function's own "
                "window definition isn't valid SQL. Partition/order by the same underlying field(s) "
                "directly instead."
            )

    def get_result(self, expression_context: ExpressionContext) -> ExpressionResult:
        # Deferred import: hare.query.queryset imports Q/Expression/... from this package at
        # import time, so a direct top-level import here would be circular (same reason as
        # elsewhere in this package).
        from hare.query.queryset import QuerySet

        orderings = [
            (read_target, order)
            for target, order in map(QuerySet._get_ordering_string, self.order_by)
            for read_target in Window.get_read_targets(expression_context, target)
        ]
        term, joins, output_field = self.expression.build(expression_context)
        if self.frame is not None:
            if not isinstance(term, WindowFrameAnalyticFunction):
                raise QueryError(
                    "Window(frame=...) takes an aggregate or a value function, not "
                    f"{type(self.expression).__name__} - it computes over the whole partition"
                )
            if self.frame.offsets_need_one_ordering and self.frame.has_offsets() and len(orderings) != 1:
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
        for partition in (
            read_target
            for target in self.partition_by
            for read_target in Window.get_read_targets(expression_context, target)
        ):
            if isinstance(partition, str):
                self._reject_nested_window_reference(expression_context, partition)
                field_result = F(partition).get_result(expression_context)
            else:
                field_result = partition.get_result(expression_context)
            EncryptedFieldBase.raise_if_encrypted(field_result.output_field, "Window(partition_by=...)")
            partition_terms.append(field_result.term)
            joins = joins + field_result.joins

        term = term.over(*partition_terms)
        for target, order in orderings:
            if isinstance(target, str):
                self._reject_nested_window_reference(expression_context, target)
                field_result = F(target).get_result(expression_context)
            else:
                field_result = target.get_result(expression_context)
            EncryptedFieldBase.raise_if_encrypted(field_result.output_field, "Window(order_by=...)")
            if self.frame is not None:
                self.frame.raise_if_unmeasured_ordering(field_result.output_field)
            term = term.orderby(field_result.term, order=order)
            joins = joins + field_result.joins

        return ExpressionResult(term=term, joins=joins, output_field=output_field)
