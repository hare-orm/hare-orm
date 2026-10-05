from __future__ import annotations

import json
from typing import Any

from hare.sql.functions.json.json_sort_key import JsonSortKey
from hare.sql.terms.criteria.criterion import Criterion
from hare.sql.terms.term import Term
from hare.sql.terms.values.value_wrapper import ValueWrapper


class SqliteJsonPathLookups:
    """The SQLite operators ordering the value of a JSON path - the value SQLite's ``json_extract()``
    reads compared with the compared value in the same form, as SQLite compares the two."""

    @staticmethod
    def get_compared_term(value: Any) -> Term:
        """A compared value as ``json_extract()`` would read it.

        Args:
            value: A number, the JSON text of any other value, or a term.

        Returns:
            The term.
        """
        if isinstance(value, Term):
            return JsonSortKey(value)
        if not isinstance(value, str):
            return ValueWrapper(value)
        decoded = json.loads(value)
        if isinstance(decoded, bool):
            decoded = int(decoded)
        elif isinstance(decoded, (dict, list)):
            decoded = json.dumps(decoded, separators=(",", ":"), ensure_ascii=False)
        return ValueWrapper(decoded)

    @classmethod
    def greater_than(cls, term: Term, value: Any) -> Criterion:
        return JsonSortKey(term) > cls.get_compared_term(value)

    @classmethod
    def greater_equal(cls, term: Term, value: Any) -> Criterion:
        return JsonSortKey(term) >= cls.get_compared_term(value)

    @classmethod
    def less_than(cls, term: Term, value: Any) -> Criterion:
        return JsonSortKey(term) < cls.get_compared_term(value)

    @classmethod
    def less_equal(cls, term: Term, value: Any) -> Criterion:
        return JsonSortKey(term) <= cls.get_compared_term(value)

    @classmethod
    def between(cls, term: Term, value: list[Any]) -> Criterion:
        lower, upper = value
        return cls.greater_equal(term, lower) & cls.less_equal(term, upper)
