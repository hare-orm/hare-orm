from typing import Any

from hare.dialects.postgresql.lookups.json.postgresql_json_filter_guards import PostgresqlJsonFilterGuards
from hare.query.filters.json_filter_guards import JsonFilterGuards
from hare.sql.enums import JSONOperators
from hare.sql.terms.array import Array
from hare.sql.terms.base.term import Term
from hare.sql.terms.base.value_wrapper import ValueWrapper
from hare.sql.terms.criteria.basic_criterion import BasicCriterion
from hare.sql.terms.criteria.criterion import Criterion


class PostgresqlJsonLookups:
    """The PostgreSQL operators of JSON lookups."""

    @staticmethod
    def contains(field: Term, value: str) -> Criterion:
        return BasicCriterion(JSONOperators.CONTAINS, field, ValueWrapper(value))

    @staticmethod
    def contained_by(field: Term, value: str) -> Criterion:
        return BasicCriterion(JSONOperators.CONTAINED_BY, field, ValueWrapper(value))

    @staticmethod
    def has_key(field: Term, value: str) -> Criterion:
        """`field__has_key=...`/`{"path__has_key": ...}` - jsonb `?`: the top-level key `value` exists
        in the object (or is a member of an array / equals a scalar string). A SQL NULL column (or
        missing path) matches nothing.

        Raises:
            UnSupportedError: `value` isn't a string.
        """
        return BasicCriterion(JSONOperators.HAS_KEY, field, ValueWrapper(JsonFilterGuards.get_key(value)))

    @staticmethod
    def has_keys(field: Term, value: list[str]) -> Criterion:
        """`field__has_keys=[...]` - jsonb `?&`: ALL of the given top-level keys exist.

        Raises:
            UnSupportedError: `value` isn't a list of strings.
        """
        return BasicCriterion(JSONOperators.HAS_KEYS, field, Array(*JsonFilterGuards.get_key_list("has_keys", value)))

    @staticmethod
    def has_any_keys(field: Term, value: list[str]) -> Criterion:
        """`field__has_any_keys=[...]` - jsonb `?|`: AT LEAST ONE of the given top-level keys exists.

        Raises:
            UnSupportedError: `value` isn't a list of strings.
        """
        return BasicCriterion(
            JSONOperators.HAS_ANY_KEYS, field, Array(*JsonFilterGuards.get_key_list("has_any_keys", value))
        )

    @staticmethod
    def filter(field: Term, value: dict[str, Any]) -> Criterion:
        return PostgresqlJsonFilterGuards.build(field, value)
