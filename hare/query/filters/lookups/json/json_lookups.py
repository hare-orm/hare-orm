"""The lookups of JSON values: JsonLookups - the operators JSONField and JSON path filters
compare with."""

from __future__ import annotations

import operator
from typing import Any

from hare.query.filters.lookups.constants import DIALECT_IMPLEMENTED_OPERATOR_MESSAGE
from hare.query.filters.lookups.dialect_implemented_operators import DialectImplementedOperators
from hare.query.filters.lookups.lookups import Lookups
from hare.sql.terms.criteria.criterion import Criterion
from hare.sql.terms.term import Term

# Operators: (field: Term, value: Any) -> Criterion.


class JsonLookups:
    """The operators of lookups on a whole JSON value."""

    @staticmethod
    def equal(term: Term, value: Any) -> Criterion:
        """``term=value`` on a JSONField - a dialect whose JSON column type doesn't compare by value
        (SQLite stores plain text) overrides this."""
        return operator.eq(term, value)

    @staticmethod
    def not_equal(term: Term, value: Any) -> Criterion:
        """``field__not=value`` on a JSONField - see ``JsonLookups.equal``."""
        return Lookups.not_equal(term, value)

    @staticmethod
    def is_in(term: Term, value: Any) -> Criterion:
        """``field__in=[...]`` on a JSONField - see ``JsonLookups.equal``."""
        return Lookups.is_in(term, value)

    @staticmethod
    def not_in(term: Term, value: Any) -> Criterion:
        """``field__not_in=[...]`` on a JSONField - see ``JsonLookups.equal``."""
        return Lookups.not_in(term, value)

    @staticmethod
    @DialectImplementedOperators.mark
    def contains(term: Term, value: str) -> Criterion:
        raise NotImplementedError(DIALECT_IMPLEMENTED_OPERATOR_MESSAGE)

    @staticmethod
    @DialectImplementedOperators.mark
    def contained_by(term: Term, value: str) -> Criterion:
        raise NotImplementedError(DIALECT_IMPLEMENTED_OPERATOR_MESSAGE)

    @staticmethod
    @DialectImplementedOperators.mark
    def filter(term: Term, value: dict[str, Any]) -> Criterion:
        raise NotImplementedError(DIALECT_IMPLEMENTED_OPERATOR_MESSAGE)

    @staticmethod
    @DialectImplementedOperators.mark
    def has_key(term: Term, value: str) -> Criterion:
        raise NotImplementedError(DIALECT_IMPLEMENTED_OPERATOR_MESSAGE)

    @staticmethod
    @DialectImplementedOperators.mark
    def has_keys(term: Term, value: list[str]) -> Criterion:
        raise NotImplementedError(DIALECT_IMPLEMENTED_OPERATOR_MESSAGE)

    @staticmethod
    @DialectImplementedOperators.mark
    def has_any_keys(term: Term, value: list[str]) -> Criterion:
        raise NotImplementedError(DIALECT_IMPLEMENTED_OPERATOR_MESSAGE)
