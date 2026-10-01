from __future__ import annotations

from typing import TYPE_CHECKING

from hare.dialects.postgresql.enums import PostgresqlRangeOperators
from hare.sql.terms.base.term import Term
from hare.sql.terms.criteria.basic_criterion import BasicCriterion
from hare.sql.terms.criteria.criterion import Criterion

if TYPE_CHECKING:  # pragma: nocoverage
    pass


class PostgresqlRangeLookups:
    """The PostgreSQL operators of range lookups."""

    @staticmethod
    def contains(field: Term, value: Term) -> Criterion:
        """`range @> range` or `range @> element` - both valid Postgres overloads, dispatched by the
        bound value's own concrete type (see PostgresqlValueEncoders.encode_range_or_element)."""
        return BasicCriterion(PostgresqlRangeOperators.CONTAINS, field, value)

    @staticmethod
    def contained_by(field: Term, value: Term) -> Criterion:
        return BasicCriterion(PostgresqlRangeOperators.CONTAINED_BY, field, value)

    @staticmethod
    def overlap(field: Term, value: Term) -> Criterion:
        return BasicCriterion(PostgresqlRangeOperators.OVERLAP, field, value)

    @staticmethod
    def fully_lt(field: Term, value: Term) -> Criterion:
        """Every value of the range is below every value of ``value``."""
        return BasicCriterion(PostgresqlRangeOperators.FULLY_LT, field, value)

    @staticmethod
    def fully_gt(field: Term, value: Term) -> Criterion:
        """Every value of the range is above every value of ``value``."""
        return BasicCriterion(PostgresqlRangeOperators.FULLY_GT, field, value)

    @staticmethod
    def not_lt(field: Term, value: Term) -> Criterion:
        """The range doesn't extend below ``value``."""
        return BasicCriterion(PostgresqlRangeOperators.NOT_LT, field, value)

    @staticmethod
    def not_gt(field: Term, value: Term) -> Criterion:
        """The range doesn't extend above ``value``."""
        return BasicCriterion(PostgresqlRangeOperators.NOT_GT, field, value)

    @staticmethod
    def adjacent_to(field: Term, value: Term) -> Criterion:
        """The range and ``value`` share a bound, without overlapping."""
        return BasicCriterion(PostgresqlRangeOperators.ADJACENT_TO, field, value)
