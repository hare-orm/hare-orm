from __future__ import annotations

from typing import TYPE_CHECKING, Any

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
from hare.query.expressions.structure.enums import ArgumentRole
from hare.query.expressions.structure.expression_structure import ExpressionStructure
from hare.query.filters.lookups.field_transforms import FieldTransforms
from hare.sql.builder.tables.array_join_source import ArrayJoinSource
from hare.sql.terms.criteria.true_criterion import TrueCriterion
from hare.sql.terms.field import Field as SqlField

if TYPE_CHECKING:
    from hare.fields.field import Field
    from hare.query.expressions.expression_result import TableCriterionTuple


class ArrayJoin(NamedJoin):
    """The elements of an array, each joined to its row under the name ``alias()``/``annotate()`` gives
    it - the row repeated with each element::

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

    structure = ExpressionStructure(positions=((0, ArgumentRole.FIELD),), keywords=(("array", ArgumentRole.FIELD),))

    repeats_rows = True

    def __init__(self, array: str | F | Expression, *, left: bool = False) -> None:
        if isinstance(array, str):
            array = F(array)
        if not isinstance(array, Expression):
            raise QueryError(
                f"ArrayJoin() joins the elements of an array - a field name, F() or an expression, got {array!r}"
            )
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
            UnSupportedError: The database has no join of an array's elements (``supports_array_join``).
        """
        if self.name is None:
            raise QueryError("An ArrayJoin is used through the name alias()/annotate() gives it")
        # A context of no connection only probes which names a query reads - the query run checks.
        connection = expression_context.connection
        if connection is not None and not connection.features.supports_array_join:
            raise UnSupportedError(
                f"ArrayJoin {self.name!r} needs a join of an array's elements to their row, which "
                f"{expression_context.dialect} doesn't have (Features.supports_array_join)"
            )
        array_result = self.array.get_result(expression_context)
        array_field = array_result.output_field
        if not isinstance(array_field, ArrayField):
            raise QueryError(
                f"ArrayJoin {self.name!r} joins the elements of an ArrayField, got {type(array_field).__name__}"
            )
        joined = ArrayJoinSource(self.name, array_result.term, left=self.left)
        # Joined like a table, with no condition.
        return [*array_result.joins, (joined, TrueCriterion())], array_field.base_field  # type: ignore[list-item]

    def get_path_result(self, path: str, expression_context: ExpressionContext) -> ExpressionResult:
        """What ``<name>__<path>`` reads - the element, with a path inside it.

        Raises:
            QueryError: The path reads nothing inside the element.
        """
        joins, element_field = self.get_joins(expression_context)
        transforms, output_field, remaining = FieldTransforms.get_path(element_field, path.split("__") if path else [])
        if remaining:
            raise QueryError(f"ArrayJoin {self.name!r}: {remaining[0]!r} reads nothing inside its element")
        term = FieldTransforms.apply(transforms, SqlField(self.name))  # type: ignore[arg-type]
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
        joins, element_field = self.get_joins(expression_context)
        segments = path.split("__") if path else []
        transforms, output_field, remaining = FieldTransforms.get_path(element_field, segments)
        result = ExpressionResult(
            term=FieldTransforms.apply(transforms, SqlField(self.name)),  # type: ignore[arg-type]
            joins=joins,
            output_field=output_field,
        )
        lookup = "__".join(remaining)
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
        """An array join repeats each row with each element."""
        return True
