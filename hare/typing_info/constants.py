from __future__ import annotations

import types
import typing

#: What a filter or ``update()`` takes besides a plain value - an expression, a term or a subquery.
ALWAYS_ACCEPTED_VALUE_FULLNAMES = (
    "hare.query.expressions.expression.Expression",
    "hare.sql.terms.term.Term",
    "hare.query.queryset.query_specification.QuerySpecification",
)
#: What ``typing.get_origin()`` gives for a union - ``X | Y`` and ``Union[X, Y]``.
UNION_ORIGINS = frozenset({types.UnionType, typing.Union})
