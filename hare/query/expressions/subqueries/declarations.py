from __future__ import annotations

from hare.query.expressions.expression import Expression


class KeyRowsQuery(Expression, abstract=True):
    """An expression that is a query of rows' keys - a value of ``pk__in=``/``<field>__in=`` like a
    queryset, its columns in the compared key's order: ``RecursiveRows``, ``CteRows``."""
