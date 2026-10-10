from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any

from hare.query.expressions.constants import NAMED_COLUMN_FILTER_ANNOTATION
from hare.query.expressions.joins.named_join import NamedJoin
from hare.query.plans.plan_origins import PlanOrigins

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.query.expressions.conditions.query_modifier import QueryModifier
    from hare.query.expressions.expression_context import ExpressionContext


class NamedColumns(NamedJoin, abstract=True):
    """A named JOIN of rows whose columns ``<name>__<column>`` reads - a filter takes the column's
    lookups (``<name>__<column>__gte``): ``Lateral``, ``merge()``'s source row."""

    def get_path_filter(
        self,
        expression_context: ExpressionContext,
        path: str,
        value: Any,
        filter_call_generation: int,
        value_origin: tuple[Any, ...] | None = None,
    ) -> QueryModifier:
        """The filter on the column ``path`` starts with - ``<column>__<lookup>``."""
        # Local imports: F and Q import the module of this base.
        from hare.query.expressions.conditions.q import Q
        from hare.query.expressions.f import F

        column_name, separator, lookup = path.partition("__")
        filter_key = f"{NAMED_COLUMN_FILTER_ANNOTATION}{separator}{lookup}"
        condition = Q(**{filter_key: value})
        condition._filter_call_generation = filter_call_generation
        if value_origin is not None:
            # The value is the filter's on the name.
            PlanOrigins.derive(condition, {filter_key: value_origin})
        column_annotations = {
            **expression_context.annotations,
            NAMED_COLUMN_FILTER_ANNOTATION: F(f"{self.name}__{column_name}"),
        }
        return condition.get_result(dataclasses.replace(expression_context, annotations=column_annotations))
