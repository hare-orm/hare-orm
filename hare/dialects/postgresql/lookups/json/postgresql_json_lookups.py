from __future__ import annotations

from typing import Any

from hare.dialects.postgresql.lookups.json.postgresql_json_filter_guards import PostgresqlJsonFilterGuards
from hare.query.filters.lookups.json.json_filter_guards import JsonFilterGuards
from hare.sql.enums import JSONOperators
from hare.sql.terms.array import Array
from hare.sql.terms.criteria.basic_criterion import BasicCriterion
from hare.sql.terms.criteria.criterion import Criterion
from hare.sql.terms.term import Term
from hare.sql.terms.values.value_wrapper import ValueWrapper


class PostgresqlJsonLookups:
    """The PostgreSQL operators of JSON lookups."""

    @staticmethod
    def contains(term: Term, value: str) -> Criterion:
        return BasicCriterion(JSONOperators.CONTAINS, term, ValueWrapper(value))

    @staticmethod
    def contained_by(term: Term, value: str) -> Criterion:
        return BasicCriterion(JSONOperators.CONTAINED_BY, term, ValueWrapper(value))

    @staticmethod
    def has_key(term: Term, value: str) -> Criterion:
        """`field__has_key=...`/`{"path__has_key": ...}` - jsonb `?`: the top-level key `value` exists
        in the object (or is a member of an array / equals a scalar string). A SQL NULL column (or
        missing path) matches nothing.

        Raises:
            UnSupportedError: `value` isn't a string.
        """
        return BasicCriterion(JSONOperators.HAS_KEY, term, ValueWrapper(JsonFilterGuards.get_key(value)))

    @staticmethod
    def has_keys(term: Term, value: list[str]) -> Criterion:
        """`field__has_keys=[...]` - jsonb `?&`: ALL of the given top-level keys exist.

        Raises:
            UnSupportedError: `value` isn't a list of strings.
        """
        return BasicCriterion(JSONOperators.HAS_KEYS, term, Array(*JsonFilterGuards.get_key_list("has_keys", value)))

    @staticmethod
    def has_any_keys(term: Term, value: list[str]) -> Criterion:
        """`field__has_any_keys=[...]` - jsonb `?|`: AT LEAST ONE of the given top-level keys exists.

        Raises:
            UnSupportedError: `value` isn't a list of strings.
        """
        return BasicCriterion(
            JSONOperators.HAS_ANY_KEYS, term, Array(*JsonFilterGuards.get_key_list("has_any_keys", value))
        )

    @staticmethod
    def filter(term: Term, value: dict[str, Any]) -> Criterion:
        return PostgresqlJsonFilterGuards.build(term, value)
