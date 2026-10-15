from __future__ import annotations

from hare.dialects.postgresql.enums import PostgresqlLtreeOperators
from hare.sql.terms.criteria.basic_criterion import BasicCriterion
from hare.sql.terms.criteria.criterion import Criterion
from hare.sql.terms.term import Term


class PostgresqlLtreeLookups:
    """The PostgreSQL operators of the ltree lookups."""

    @staticmethod
    def ancestor_of(term: Term, value: Term) -> Criterion:
        """The path is ``value`` or one of its ancestors."""
        return BasicCriterion(PostgresqlLtreeOperators.ANCESTOR_OF, term, value)

    @staticmethod
    def descendant_of(term: Term, value: Term) -> Criterion:
        """The path is ``value`` or one of its descendants."""
        return BasicCriterion(PostgresqlLtreeOperators.DESCENDANT_OF, term, value)

    @staticmethod
    def matches(term: Term, value: Term) -> Criterion:
        """The path matches the ``lquery`` pattern."""
        return BasicCriterion(PostgresqlLtreeOperators.MATCHES, term, value)

    @staticmethod
    def matches_any(term: Term, value: Term) -> Criterion:
        """The path matches one of an array of ``lquery`` patterns."""
        return BasicCriterion(PostgresqlLtreeOperators.MATCHES_ANY, term, value)

    @staticmethod
    def matches_text(term: Term, value: Term) -> Criterion:
        """The path's labels match the ``ltxtquery`` full-text-like query."""
        return BasicCriterion(PostgresqlLtreeOperators.MATCHES_TEXT, term, value)
