from __future__ import annotations

import contextvars
from typing import TYPE_CHECKING

from hare.query.expressions.base.expression_context import ExpressionContext
from hare.sql.terms.base.term import Term

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.query.expressions.base.expression_result import TableCriterionTuple


# The outer query's ExpressionContext while the SQL of a child queryset in Exists(...)/Subquery(...)
# is built - an OuterRef(...) reads the outer table through it. The build is synchronous, so the
# value never leaks to another coroutine.
outer_expression_context: contextvars.ContextVar[ExpressionContext | None] = contextvars.ContextVar(
    "outer_expression_context", default=None
)


# The joins a `related__field` OuterRef(...) needs the outer query to make - found while the child
# query is built, collected here.
outer_extra_joins: contextvars.ContextVar[list[TableCriterionTuple] | None] = contextvars.ContextVar(
    "_outer_extra_joins", default=None
)


# The names of the outer query's aggregate annotations an OuterRef(...) references. The child query
# is then evaluated per group of the outer one: its term is an aggregate there - not grouped by,
# filtered in HAVING.
outer_aggregate_references: contextvars.ContextVar[list[str] | None] = contextvars.ContextVar(
    "_outer_aggregate_references", default=None
)


# The outer query's columns an OuterRef(...) reads - the outer query groups by them, which keeps the
# correlated subquery valid next to an aggregate.
outer_reference_terms: contextvars.ContextVar[list[Term] | None] = contextvars.ContextVar(
    "_outer_reference_terms", default=None
)
