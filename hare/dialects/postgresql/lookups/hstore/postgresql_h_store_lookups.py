from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.dialects.postgresql.enums import HStoreOperators
from hare.sql.functions.cast import Cast
from hare.sql.terms.base.term import Term
from hare.sql.terms.base.value_wrapper import ValueWrapper
from hare.sql.terms.criteria.basic_criterion import BasicCriterion

if TYPE_CHECKING:  # pragma: nocoverage
    pass


class PostgresqlHStoreLookups:
    """The PostgreSQL operators of hstore lookups."""

    @staticmethod
    def contains(field: Term, value: Any) -> BasicCriterion:
        """The value holds every pair of ``value``."""
        return BasicCriterion(HStoreOperators.CONTAINS, field, field.wrap_constant(value))

    @staticmethod
    def contained_by(field: Term, value: Any) -> BasicCriterion:
        """Every pair of the value is in ``value``."""
        return BasicCriterion(HStoreOperators.CONTAINED_BY, field, field.wrap_constant(value))

    @staticmethod
    def has_key(field: Term, value: Any) -> BasicCriterion:
        """The value has the key."""
        return BasicCriterion(HStoreOperators.HAS_KEY, field, Cast(field.wrap_constant(value), "text"))

    @staticmethod
    def has_keys(field: Term, value: Any) -> BasicCriterion:
        """The value has every key."""
        return BasicCriterion(HStoreOperators.HAS_KEYS, field, Cast(ValueWrapper(value), "text[]"))

    @staticmethod
    def has_any_keys(field: Term, value: Any) -> BasicCriterion:
        """The value has one of the keys at least."""
        return BasicCriterion(HStoreOperators.HAS_ANY_KEYS, field, Cast(ValueWrapper(value), "text[]"))
