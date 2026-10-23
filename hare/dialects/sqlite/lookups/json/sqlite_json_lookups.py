from __future__ import annotations

from typing import Any

from hare.dialects.sqlite.lookups.json.sqlite_json_filter_guards import SqliteJsonFilterGuards
from hare.exceptions import UnSupportedError
from hare.query.filters import JsonLookups
from hare.sql.terms.criteria.criterion import Criterion
from hare.sql.terms.term import Term


class SqliteJsonLookups:
    """The SQLite operators of JSON lookups."""

    @staticmethod
    def contains(term: Term, value: str) -> Criterion:
        """`field__contains=...` on a `JSONField` - refused: SQLite's JSON has no containment.

        Raises:
            UnSupportedError: Always.
        """
        raise UnSupportedError("JSONField __contains needs a JSON containment, which SQLite doesn't have")

    @staticmethod
    def contained_by(term: Term, value: str) -> Criterion:
        """`field__contained_by=...` on a `JSONField` - refused: SQLite's JSON has no containment.

        Raises:
            UnSupportedError: Always.
        """
        raise UnSupportedError("JSONField __contained_by needs a JSON containment, which SQLite doesn't have")

    @staticmethod
    def filter(term: Term, value: dict[str, Any]) -> Criterion:
        """`field__filter=...` on a `JSONField`."""
        return SqliteJsonFilterGuards.build(term, value)

    @staticmethod
    def has_key(term: Term, value: str) -> Criterion:
        """`field__has_key=...` on a `JSONField`."""
        return SqliteJsonFilterGuards.key_existence_criterion(term, [], JsonLookups.has_key, value)

    @staticmethod
    def has_keys(term: Term, value: list[str]) -> Criterion:
        """`field__has_keys=...` on a `JSONField`."""
        return SqliteJsonFilterGuards.key_existence_criterion(term, [], JsonLookups.has_keys, value)

    @staticmethod
    def has_any_keys(term: Term, value: list[str]) -> Criterion:
        """`field__has_any_keys=...` on a `JSONField`."""
        return SqliteJsonFilterGuards.key_existence_criterion(term, [], JsonLookups.has_any_keys, value)
