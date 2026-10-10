from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from hare.query.expressions.expression_context import ExpressionContext
from hare.sql.terms.term import Term

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.query.expressions.expression_result import TableCriterionTuple
from hare.query.expressions.subqueries.outer_query_state import (
    outer_aggregate_references,
    outer_expression_context,
    outer_extra_joins,
    outer_reference_terms,
)


@dataclass
class OuterScope:
    """What building a child query nested in a Subquery(...) / Exists(...) collects about the outer
    query, through the four contextvars above - set for the build by ``entered()``."""

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
        outer_token = outer_expression_context.set(expression_context)
        joins_token = outer_extra_joins.set(scope.extra_joins)
        aggregate_references_token = outer_aggregate_references.set(scope.aggregate_references)
        reference_terms_token = outer_reference_terms.set(scope.reference_terms)
        try:
            yield scope
        finally:
            outer_expression_context.reset(outer_token)
            outer_extra_joins.reset(joins_token)
            outer_aggregate_references.reset(aggregate_references_token)
            outer_reference_terms.reset(reference_terms_token)
