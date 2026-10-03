from typing import Any

from hare.dialects.sqlite.functions.json.sqlite_json_containment import SqliteJsonContainment
from hare.dialects.sqlite.functions.json.sqlite_json_filter_guards import SqliteJsonFilterGuards
from hare.query.filters import JsonLookups
from hare.sql.terms.base.term import Term
from hare.sql.terms.base.value_wrapper import ValueWrapper
from hare.sql.terms.criteria.criterion import Criterion


class SqliteJsonLookups:
    """The SQLite operators of JSON lookups."""

    @staticmethod
    def contains(field: Term, value: str) -> Criterion:
        """`field__contains=...` on a `JSONField` - Postgres jsonb `@>` containment."""
        return SqliteJsonContainment.get_criterion(field, ValueWrapper(value))

    @staticmethod
    def contained_by(field: Term, value: str) -> Criterion:
        """`field__contained_by=...` on a `JSONField` - Postgres jsonb `<@` containment."""
        return SqliteJsonContainment.get_criterion(ValueWrapper(value), field)

    @staticmethod
    def filter(field: Term, value: dict[str, Any]) -> Criterion:
        """`field__filter=...` on a `JSONField`."""
        return SqliteJsonFilterGuards.build(field, value)

    @staticmethod
    def has_key(field: Term, value: str) -> Criterion:
        """`field__has_key=...` on a `JSONField`."""
        return SqliteJsonFilterGuards.key_existence_criterion(field, [], JsonLookups.has_key, value)

    @staticmethod
    def has_keys(field: Term, value: list[str]) -> Criterion:
        """`field__has_keys=...` on a `JSONField`."""
        return SqliteJsonFilterGuards.key_existence_criterion(field, [], JsonLookups.has_keys, value)

    @staticmethod
    def has_any_keys(field: Term, value: list[str]) -> Criterion:
        """`field__has_any_keys=...` on a `JSONField`."""
        return SqliteJsonFilterGuards.key_existence_criterion(field, [], JsonLookups.has_any_keys, value)
