from __future__ import annotations

from typing import TYPE_CHECKING

from hare.exceptions import QueryError
from hare.fields.encrypted.encrypted_field_base import EncryptedFieldBase
from hare.query.expressions.expression_result import ExpressionResult
from hare.query.expressions.f import F
from hare.query.key_columns import KeyColumns
from hare.query.lookup_info.bound_lookup_path import BoundLookupPath
from hare.query.queryset.concrete_field_paths import ConcreteFieldPaths

if TYPE_CHECKING:
    from hare.query.expressions.expression_context import ExpressionContext


class GroupingExpressions:
    """Bind the physical grouping expressions of one logical field or annotation.

    GROUP BY and GROUPING read the same inputs: a composite key contributes several
    columns together, while an annotation retains its already bound parameter wrappers.
    """

    @staticmethod
    def get_results(context: ExpressionContext, name: str) -> list[ExpressionResult]:
        """Return the grouped inputs and their joins without converting column values."""
        concrete_name = KeyColumns.get_primary_key_name(context.binding, context.annotations, name)
        if name in context.annotations and concrete_name == name:
            if name in context.annotation_names_in_progress:
                raise QueryError(
                    f"Circular annotation reference detected: {name!r} depends on itself "
                    "(directly or through another annotation)"
                )
            get_result = context.grouping_annotation_result
            return [get_result(name, context) if get_result is not None else F(name).get_result(context)]
        results = []
        for path in ConcreteFieldPaths.get_paths(context.binding, concrete_name):
            bound = BoundLookupPath.bind(context, path)
            EncryptedFieldBase.raise_if_encrypted(bound.field, "GROUP BY")
            results.append(ExpressionResult(term=bound.column, joins=list(bound.joins), output_field=bound.field))
        return results
