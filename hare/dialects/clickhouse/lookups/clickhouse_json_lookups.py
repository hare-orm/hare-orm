from __future__ import annotations

from typing import Any

from hare.dialects.clickhouse.lookups.clickhouse_json_containment import ClickhouseJsonContainment
from hare.dialects.clickhouse.lookups.clickhouse_json_key_existence import ClickhouseJsonKeyExistence
from hare.query.filters.lookups.json.json_filter_guards import JsonFilterGuards
from hare.sql.enums import Equality
from hare.sql.terms.criteria.basic_criterion import BasicCriterion
from hare.sql.terms.criteria.criterion import Criterion
from hare.sql.terms.term import Term
from hare.sql.terms.values.value_wrapper import ValueWrapper


class ClickhouseJsonLookups:
    """The ClickHouse operators of the JSON key lookups - over the JSON text the column holds."""

    @staticmethod
    def get_criterion(existence: ClickhouseJsonKeyExistence) -> Criterion:
        """The test as a condition - true where the keys exist."""
        return BasicCriterion(Equality.EQ, existence, ValueWrapper(1, allow_parametrize=False))

    @staticmethod
    def contains(term: Term, value: str) -> Criterion:
        """`field__contains=...` on a `JSONField` - PostgreSQL's jsonb `@>`."""
        return ClickhouseJsonContainment.get_criterion(term, value, contains=True)

    @staticmethod
    def contained_by(term: Term, value: str) -> Criterion:
        """`field__contained_by=...` on a `JSONField` - PostgreSQL's jsonb `<@`."""
        return ClickhouseJsonContainment.get_criterion(term, value, contains=False)

    @classmethod
    def has_key(cls, term: Term, value: Any) -> Criterion:
        """`field__has_key=...` on a `JSONField`."""
        key = ValueWrapper(JsonFilterGuards.get_key(value))
        return cls.get_criterion(ClickhouseJsonKeyExistence(term, key, every_key=True, single_key=True))

    @classmethod
    def has_keys(cls, term: Term, value: Any) -> Criterion:
        """`field__has_keys=...` on a `JSONField`."""
        keys = ValueWrapper(JsonFilterGuards.get_key_list("has_keys", value))
        return cls.get_criterion(ClickhouseJsonKeyExistence(term, keys, every_key=True, single_key=False))

    @classmethod
    def has_any_keys(cls, term: Term, value: Any) -> Criterion:
        """`field__has_any_keys=...` on a `JSONField`."""
        keys = ValueWrapper(JsonFilterGuards.get_key_list("has_any_keys", value))
        return cls.get_criterion(ClickhouseJsonKeyExistence(term, keys, every_key=False, single_key=False))
