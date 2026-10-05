from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from hare.exceptions import QueryError, UnSupportedError
from hare.fields.data.containers.array_field import ArrayField
from hare.query.expressions.conditions.q import Q
from hare.query.expressions.conditions.query_modifier import QueryModifier
from hare.query.expressions.expression import Expression
from hare.query.expressions.expression_context import ExpressionContext
from hare.query.expressions.expression_result import ExpressionResult
from hare.query.expressions.f import F
from hare.query.expressions.joins.array_join_element import ArrayJoinElement
from hare.query.expressions.joins.named_join import NamedJoin
from hare.sql.builder.tables.array_join_source import ArrayJoinSource
from hare.sql.terms.criteria.true_criterion import TrueCriterion
from hare.sql.terms.field import Field as SqlField

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.fields.field import Field
    from hare.query.expressions.expression_result import TableCriterionTuple
    from hare.sql.builder.tables.table import Table


class ArrayJoin(NamedJoin):
    """The elements of an array, each joined to its row under the name ``alias()``/``annotate()`` gives
    it - the row repeated with each element (``ARRAY JOIN``)::

        Shipment.objects.alias(tag=ArrayJoin("tags")).filter(tag__startswith="a").values("id", "tag")

    The name reads the element in filters, expressions, ``values()`` and ``order_by()``, and
    ``<name>__<path>`` reads inside it as a path of the array's ``base_field`` does (``item__sku`` of
    an array of named tuples). A row with an empty array comes with none of its elements - with
    ``left=True`` once, its element the element type's default. As a JOIN of a to-many relation, the
    repeated rows are what a filter, a count or an aggregate reads. A database without
    ``Features.supports_array_join`` raises ``UnSupportedError`` before the query is sent.

    Args:
        array: The array - a field name, ``F()`` or an expression of an ``ArrayField``.
        left: Whether a row with an empty array is kept.

    Raises:
        QueryError: ``array`` is no field name, ``F()`` or expression.
    """

    def __init__(self, array: str | F | Expression, *, left: bool = False) -> None:
        if isinstance(array, str):
            array = F(array)
        if not isinstance(array, Expression):
            raise QueryError(
                f"ArrayJoin() joins the elements of an array - a field name, F() or an expression, got {array!r}"
            )
        if type(left) is not bool:
            raise QueryError(f"ArrayJoin(left=...) takes a bool, got {left!r}")
        self.array = array
        self.left = left

    def get_joins(self, expression_context: ExpressionContext) -> tuple[list[TableCriterionTuple], Field[Any]]:
        """The JOINs - those the array's path needs, then the array's elements - and the field of an
        element.

        Args:
            expression_context: The context of the queried model.

        Returns:
            The JOINs and the element's field.

        Raises:
            QueryError: It is used before ``alias()``/``annotate()`` named it, or the array isn't an
                ``ArrayField``.
            UnSupportedError: The database has no ``ARRAY JOIN``.
        """
        if self.name is None:
            raise QueryError("An ArrayJoin is used through the name alias()/annotate() gives it")
        # A context of no connection only probes which names a query reads - the query run checks.
        connection = expression_context.connection
        if connection is not None and not connection.features.supports_array_join:
            raise UnSupportedError(
                f"ArrayJoin {self.name!r} needs an ARRAY JOIN, which {expression_context.dialect} doesn't have"
            )
        array_result = self.array.get_result(expression_context)
        array_field = array_result.output_field  # type: ignore[call-overload]
        if not isinstance(array_field, ArrayField):
            raise QueryError(f"ArrayJoin {self.name!r} joins the elements of an ArrayField, got {array_field!r}")
        joined = ArrayJoinSource(self.name, array_result.term, left=self.left)
        # Joined like a table, with no condition.
        return [*array_result.joins, (cast("Table", joined), TrueCriterion())], array_field.base_field

    def get_path_result(self, path: str, expression_context: ExpressionContext) -> ExpressionResult:
        """What ``<name>__<path>`` reads - the element, with a path inside it.

        Raises:
            QueryError: The path reads nothing inside the element.
        """
        joins, element_field = self.get_joins(expression_context)
        term: Any = SqlField(cast("str", self.name))
        output_field = element_field
        for segment in path.split("__") if path else ():
            transform = output_field.get_path_transform(segment)
            if transform is None:
                raise QueryError(f"ArrayJoin {self.name!r}: {segment!r} reads nothing inside its element")
            get_term, output_field = transform
            term = get_term(term)
        return ExpressionResult(term=term, joins=joins, output_field=output_field)

    def get_path_filter(
        self,
        expression_context: ExpressionContext,
        path: str,
        value: Any,
        filter_call_generation: int,
        value_origin: tuple[Any, ...] | None = None,
    ) -> QueryModifier:
        """The JOIN, then the filter on the element - or on a path inside it, the segments after the
        path its lookup."""
        _, element_field = self.get_joins(expression_context)
        segments = path.split("__") if path else []
        path_length = 0
        field: Any = element_field
        while path_length < len(segments):
            transform = field.get_path_transform(segments[path_length])
            if transform is None:
                break
            field = transform[1]
            path_length += 1
        result = self.get_path_result("__".join(segments[:path_length]), expression_context)
        lookup = "__".join(segments[path_length:])
        condition = Q(**{f"{self.name}__{lookup}" if lookup else str(self.name): value})
        condition._filter_call_generation = filter_call_generation
        modifier = condition.get_result(
            ExpressionContext(
                model=expression_context.model,
                dialect=expression_context.dialect,
                connection=expression_context.connection,
                table=expression_context.table,
                annotations={**expression_context.annotations, str(self.name): ArrayJoinElement(result)},
                visibility=expression_context.visibility,
                value_wrapper_references=expression_context.value_wrapper_references,
            )
        )
        return QueryModifier(joins=result.joins) & modifier

    def is_multi_valued(self, model: Any) -> bool:
        """An ``ARRAY JOIN`` repeats each row with each element."""
        return True
