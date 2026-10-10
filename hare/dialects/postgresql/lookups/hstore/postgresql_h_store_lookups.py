from __future__ import annotations

from typing import Any

from hare.dialects.postgresql.enums import HStoreOperators
from hare.sql.functions.cast import Cast
from hare.sql.terms.criteria.basic_criterion import BasicCriterion
from hare.sql.terms.term import Term
from hare.sql.terms.values.value_wrapper import ValueWrapper


class PostgresqlHStoreLookups:
    """The PostgreSQL operators of hstore lookups."""

    @staticmethod
    def contains(term: Term, value: Any) -> BasicCriterion:
        """The value holds every pair of ``value``."""
        return BasicCriterion(HStoreOperators.CONTAINS, term, term.wrap_constant(value))

    @staticmethod
    def contained_by(term: Term, value: Any) -> BasicCriterion:
        """Every pair of the value is in ``value``."""
        return BasicCriterion(HStoreOperators.CONTAINED_BY, term, term.wrap_constant(value))

    @staticmethod
    def has_key(term: Term, value: Any) -> BasicCriterion:
        """The value has the key."""
        return BasicCriterion(HStoreOperators.HAS_KEY, term, Cast(term.wrap_constant(value), "text"))

    @staticmethod
    def has_keys(term: Term, value: Any) -> BasicCriterion:
        """The value has every key."""
        return BasicCriterion(HStoreOperators.HAS_KEYS, term, Cast(ValueWrapper(value), "text[]"))

    @staticmethod
    def has_any_keys(term: Term, value: Any) -> BasicCriterion:
        """The value has one of the keys at least."""
        return BasicCriterion(HStoreOperators.HAS_ANY_KEYS, term, Cast(ValueWrapper(value), "text[]"))
