from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any

from hare.query.expressions.constants import NAMED_COLUMN_FILTER_ANNOTATION
from hare.query.expressions.joins.named_join import NamedJoin

if TYPE_CHECKING:
    from hare.query.expressions.conditions.query_modifier import QueryModifier
    from hare.query.expressions.expression_context import ExpressionContext


class NamedColumns(NamedJoin, abstract=True):
    """A named JOIN of rows whose columns ``<name>__<column>`` reads - a filter takes the column's
    lookups (``<name>__<column>__gte``): ``Lateral``, ``JsonTable``, ``merge()``'s source row."""

    def get_path_filter(
        self,
        expression_context: ExpressionContext,
        path: str,
        value: Any,
        filter_call_generation: int,
        value_origin: tuple[Any, ...] | None = None,
    ) -> QueryModifier:
        """The filter on the column ``path`` starts with - ``<column>__<lookup>``."""
        # Local import: F imports the module of this base.
        from hare.query.expressions.f import F

        column_name, separator, lookup = path.partition("__")
        filter_key = f"{NAMED_COLUMN_FILTER_ANNOTATION}{separator}{lookup}"
        condition = self._get_filter_condition(filter_key, value, filter_call_generation, value_origin)
        column_annotations = {
            **expression_context.annotations,
            NAMED_COLUMN_FILTER_ANNOTATION: F(f"{self.name}__{column_name}"),
        }
        return condition.get_result(dataclasses.replace(expression_context, annotations=column_annotations))
