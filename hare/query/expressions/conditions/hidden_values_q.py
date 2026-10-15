from __future__ import annotations

from hare.query.expressions.conditions.q import Q
from hare.query.expressions.conditions.query_modifier import QueryModifier
from hare.query.expressions.expression_context import ExpressionContext
from hare.query.filters.resolution.filter_kwargs import FilterKwargs


class HiddenValuesQ(Q):
    """A ``Q`` none of whose values a log, event or error shows (``QueryParameters``) - a filter by the
    values of a ``sensitive=True`` field on fields that aren't sensitive themselves, as hare's check
    of a relation's values against the related rows. Its plan is its own: the description of a
    ``Q`` starts with its class."""

    __slots__ = ()

    def get_result(self, expression_context: ExpressionContext) -> QueryModifier:
        """Resolves the condition, every value it binds hidden.

        Args:
            expression_context: The context the condition is resolved against.

        Returns:
            The parts of the statement.
        """
        modifier = super().get_result(expression_context)
        FilterKwargs.hide_sensitive_values(modifier.where_criterion)
        FilterKwargs.hide_sensitive_values(modifier.having_criterion)
        return modifier
