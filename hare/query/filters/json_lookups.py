"""The lookups of JSON values: JsonLookups - the operators JSONField and JSON path filters
compare with."""

from __future__ import annotations

import operator
from typing import Any

from hare.query.filters.dialect_implemented_operators import DialectImplementedOperators
from hare.query.filters.lookups import Lookups
from hare.sql.terms.base.term import Term
from hare.sql.terms.criteria.criterion import Criterion

# Operators: (field: Term, value: Any) -> Criterion.


class JsonLookups:
    """The operators of lookups on a whole JSON value."""

    @staticmethod
    def equal(field: Term, value: Any) -> Criterion:
        """``field=value`` on a JSONField - a dialect whose JSON column type doesn't compare by value
        (SQLite stores plain text) overrides this."""
        return operator.eq(field, value)

    @staticmethod
    def not_equal(field: Term, value: Any) -> Criterion:
        """``field__not=value`` on a JSONField - see ``JsonLookups.equal``."""
        return Lookups.not_equal(field, value)

    @staticmethod
    def is_in(field: Term, value: Any) -> Criterion:
        """``field__in=[...]`` on a JSONField - see ``JsonLookups.equal``."""
        return Lookups.is_in(field, value)

    @staticmethod
    def not_in(field: Term, value: Any) -> Criterion:
        """``field__not_in=[...]`` on a JSONField - see ``JsonLookups.equal``."""
        return Lookups.not_in(field, value)

    @staticmethod
    @DialectImplementedOperators.mark
    def contains(field: Term, value: str) -> Criterion:
        raise NotImplementedError("must be overridden in each executor")

    @staticmethod
    @DialectImplementedOperators.mark
    def contained_by(field: Term, value: str) -> Criterion:
        raise NotImplementedError("must be overridden in each executor")

    @staticmethod
    @DialectImplementedOperators.mark
    def filter(field: Term, value: dict[str, Any]) -> Criterion:
        raise NotImplementedError("must be overridden in each executor")

    @staticmethod
    @DialectImplementedOperators.mark
    def has_key(field: Term, value: str) -> Criterion:
        raise NotImplementedError("must be overridden in each executor")

    @staticmethod
    @DialectImplementedOperators.mark
    def has_keys(field: Term, value: list[str]) -> Criterion:
        raise NotImplementedError("must be overridden in each executor")

    @staticmethod
    @DialectImplementedOperators.mark
    def has_any_keys(field: Term, value: list[str]) -> Criterion:
        raise NotImplementedError("must be overridden in each executor")
