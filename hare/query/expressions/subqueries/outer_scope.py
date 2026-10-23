from __future__ import annotations

import contextvars
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar

from hare.query.expressions.expression_context import ExpressionContext
from hare.sql.terms.term import Term

if TYPE_CHECKING:
    from hare.query.expressions.expression_result import TableCriterionTuple


@dataclass
class OuterScope:
    """What building a child query nested in a Subquery(...) / Exists(...) collects about the outer
    query - set for the build by ``entered()``. The build is synchronous, so the values never leak to
    another coroutine."""

    #: The outer query's ExpressionContext while the SQL of a child queryset is built - an
    #: OuterReference(...) reads the outer table through it.
    current_expression_context: ClassVar[contextvars.ContextVar[ExpressionContext | None]] = contextvars.ContextVar(
        "hare_outer_expression_context", default=None
    )
    #: The joins a ``related__field`` OuterReference(...) needs the outer query to make - found while the
    #: child query is built, collected here.
    current_extra_joins: ClassVar[contextvars.ContextVar[list[TableCriterionTuple] | None]] = contextvars.ContextVar(
        "hare_outer_extra_joins", default=None
    )
    #: The names of the outer query's aggregate annotations an OuterReference(...) references. The child
    #: query is then evaluated per group of the outer one: its term is an aggregate there - not grouped by,
    #: filtered in HAVING.
    current_aggregate_references: ClassVar[contextvars.ContextVar[list[str] | None]] = contextvars.ContextVar(
        "hare_outer_aggregate_references", default=None
    )
    #: The outer query's columns an OuterReference(...) reads - the outer query groups by them, which keeps
    #: the correlated subquery valid next to an aggregate.
    current_reference_terms: ClassVar[contextvars.ContextVar[list[Term] | None]] = contextvars.ContextVar(
        "hare_outer_reference_terms", default=None
    )

    #: The joins the outer query needs for the child's ``OuterReference("related__field")``.
    extra_joins: list[TableCriterionTuple] = field(default_factory=list)
    #: The outer aggregate annotations the child references.
    aggregate_references: list[str] = field(default_factory=list)
    #: The outer columns the child reads.
    reference_terms: list[Term] = field(default_factory=list)

    @classmethod
    @contextmanager
    def entered(cls, expression_context: ExpressionContext) -> Generator[OuterScope]:
        """Makes ``expression_context`` the outer query of the child built inside the block, and
        collects what the build reports about it.

        Args:
            expression_context: The outer query's resolve context.

        Yields:
            The scope, filled once the block ends.
        """
        scope = cls()
        outer_token = cls.current_expression_context.set(expression_context)
        joins_token = cls.current_extra_joins.set(scope.extra_joins)
        aggregate_references_token = cls.current_aggregate_references.set(scope.aggregate_references)
        reference_terms_token = cls.current_reference_terms.set(scope.reference_terms)
        try:
            yield scope
        finally:
            cls.current_expression_context.reset(outer_token)
            cls.current_extra_joins.reset(joins_token)
            cls.current_aggregate_references.reset(aggregate_references_token)
            cls.current_reference_terms.reset(reference_terms_token)
