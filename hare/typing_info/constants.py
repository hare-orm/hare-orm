from __future__ import annotations

#: What a filter or ``update()`` takes besides a plain value - an expression, a term or a subquery.
ALWAYS_ACCEPTED_VALUE_FULLNAMES = (
    "hare.query.expressions.expression.Expression",
    "hare.sql.terms.term.Term",
    "hare.query.queryset.query_specification.QuerySpecification",
)
