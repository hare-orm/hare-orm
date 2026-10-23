from __future__ import annotations

from hare.dialects.postgresql.enums import PostgresqlNetworkOperators
from hare.sql.terms.criteria.basic_criterion import BasicCriterion
from hare.sql.terms.criteria.criterion import Criterion
from hare.sql.terms.term import Term


class PostgresqlNetworkLookups:
    """The PostgreSQL operators of the subnet lookups of ``inet``/``cidr`` columns."""

    @staticmethod
    def contained(term: Term, value: Term) -> Criterion:
        """The address or network lies inside ``value``, not equal to it."""
        return BasicCriterion(PostgresqlNetworkOperators.CONTAINED, term, value)

    @staticmethod
    def contained_or_equal(term: Term, value: Term) -> Criterion:
        """The address or network lies inside ``value`` or equals it."""
        return BasicCriterion(PostgresqlNetworkOperators.CONTAINED_OR_EQUAL, term, value)

    @staticmethod
    def contains(term: Term, value: Term) -> Criterion:
        """The network holds ``value``, not equal to it."""
        return BasicCriterion(PostgresqlNetworkOperators.CONTAINS, term, value)

    @staticmethod
    def contains_or_equal(term: Term, value: Term) -> Criterion:
        """The network holds ``value`` or equals it."""
        return BasicCriterion(PostgresqlNetworkOperators.CONTAINS_OR_EQUAL, term, value)

    @staticmethod
    def overlaps(term: Term, value: Term) -> Criterion:
        """One of the two holds the other or equals it."""
        return BasicCriterion(PostgresqlNetworkOperators.OVERLAPS, term, value)
