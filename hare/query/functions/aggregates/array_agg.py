from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.dialects.base.constants import SQL_DIALECT
from hare.exceptions import UnSupportedError
from hare.fields.data.containers.array_field import ArrayField
from hare.fields.field import Field
from hare.query.expressions import (
    Aggregate,
)

if TYPE_CHECKING:
    from hare.query.expressions.expression_context import ExpressionContext
    from hare.query.expressions.expression_result import ExpressionResult


class ArrayAgg(Aggregate):
    """``ARRAY_AGG(field)`` - a column's values of each group as an array, each element decoded through
    the aggregated field; a NULL is an element too. On a database with arrays, each writing it its own
    way.

    Example: ``Model.objects.annotate(tags=ArrayAgg("tag__name", distinct=True)).group_by("id")``

    Raises:
        UnSupportedError: The database has no arrays - before the query is sent.
    """

    function_name = "ARRAY_AGG"
    populate_field_object = True
    allows_order_by = True

    def _coerce_output_field(self, field_object: Field[Any], expression_context: ExpressionContext) -> Field[Any]:
        return ArrayField(base_field=field_object)

    def get_result(self, expression_context: ExpressionContext) -> ExpressionResult:
        result = super().get_result(expression_context)
        output_field = result.output_field
        # The neutral dialect a query's shape is analysed with has no arrays - the query's own
        # dialect is checked when it is built.
        if output_field is not None and expression_context.dialect is not SQL_DIALECT:
            try:
                output_field.get_column_type(expression_context.dialect)
            except UnSupportedError:
                raise UnSupportedError(
                    f"ArrayAgg() gives an array, which the {expression_context.dialect.name} database has none of"
                ) from None
        return result
