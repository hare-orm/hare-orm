from __future__ import annotations

from hare.dialects.postgresql.enums import PostgresqlRangeOperators
from hare.sql.terms.criteria.basic_criterion import BasicCriterion
from hare.sql.terms.criteria.criterion import Criterion
from hare.sql.terms.term import Term


class PostgresqlRangeLookups:
    """The PostgreSQL operators of range lookups."""

    @staticmethod
    def contains(term: Term, value: Term) -> Criterion:
        """`range @> range` or `range @> element` - both valid Postgres overloads, dispatched by the
        bound value's own concrete type (see PostgresqlValueEncoders.encode_range_or_element)."""
        return BasicCriterion(PostgresqlRangeOperators.CONTAINS, term, value)

    @staticmethod
    def contained_by(term: Term, value: Term) -> Criterion:
        return BasicCriterion(PostgresqlRangeOperators.CONTAINED_BY, term, value)

    @staticmethod
    def overlap(term: Term, value: Term) -> Criterion:
        return BasicCriterion(PostgresqlRangeOperators.OVERLAP, term, value)

    @staticmethod
    def fully_lt(term: Term, value: Term) -> Criterion:
        """Every value of the range is below every value of ``value``."""
        return BasicCriterion(PostgresqlRangeOperators.FULLY_LT, term, value)

    @staticmethod
    def fully_gt(term: Term, value: Term) -> Criterion:
        """Every value of the range is above every value of ``value``."""
        return BasicCriterion(PostgresqlRangeOperators.FULLY_GT, term, value)

    @staticmethod
    def not_lt(term: Term, value: Term) -> Criterion:
        """The range doesn't extend below ``value``."""
        return BasicCriterion(PostgresqlRangeOperators.NOT_LT, term, value)

    @staticmethod
    def not_gt(term: Term, value: Term) -> Criterion:
        """The range doesn't extend above ``value``."""
        return BasicCriterion(PostgresqlRangeOperators.NOT_GT, term, value)

    @staticmethod
    def adjacent_to(term: Term, value: Term) -> Criterion:
        """The range and ``value`` share a bound, without overlapping."""
        return BasicCriterion(PostgresqlRangeOperators.ADJACENT_TO, term, value)
