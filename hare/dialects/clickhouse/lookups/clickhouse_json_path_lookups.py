from __future__ import annotations

from typing import Any

from hare.dialects.clickhouse.lookups.clickhouse_json_sort_key import ClickhouseJsonSortKey
from hare.dialects.clickhouse.lookups.clickhouse_json_value_text import ClickhouseJsonValueText
from hare.dialects.clickhouse.lookups.declarations import ClickhouseJsonNullText
from hare.query.filters.lookups.lookups import Lookups
from hare.sql.terms.criteria.criterion import Criterion
from hare.sql.terms.term import Term
from hare.sql.terms.values.value_wrapper import ValueWrapper


class ClickhouseJsonPathLookups:
    """The ClickHouse operators of the lookups of a JSON path value - over the JSON text the path
    reads: a value compared is re-written as ClickHouse writes that text, and values are ordered by
    their ``ClickhouseJsonSortKey``, as PostgreSQL orders ``jsonb``."""

    @staticmethod
    def equal(term: Term, value: Any) -> Criterion:
        """The path's value is ``value`` - the JSON null of a JSON column is a missing path."""
        if isinstance(value, ClickhouseJsonNullText):
            return term.isnull()
        return term == value

    @staticmethod
    def not_equal(term: Term, value: Any) -> Criterion:
        """The path's value isn't ``value`` - the JSON null of a JSON column is a missing path."""
        if isinstance(value, ClickhouseJsonNullText):
            return term.notnull()
        return Lookups.not_equal(term, value)

    @staticmethod
    def get_value_text(value: Any) -> Term:
        """A compared value as the text a path reads.

        Args:
            value: Its JSON text, or a term holding it already.

        Returns:
            The term.
        """
        return value if isinstance(value, Term) else ClickhouseJsonValueText(ValueWrapper(value))

    @classmethod
    def compare_keys(cls, term: Term, value: Any, comparison: str) -> Criterion:
        """The path's value compared with ``value`` by their keys - a missing value compares with
        nothing.

        Args:
            term: The path.
            value: The JSON text compared with.
            comparison: The name of the term's comparison method - ``gt``, ``gte``, ``lt``, ``lte``.

        Returns:
            The condition.
        """
        compared = getattr(ClickhouseJsonSortKey(term), comparison)(ClickhouseJsonSortKey(cls.get_value_text(value)))
        return term.notnull() & compared

    @classmethod
    def greater_than(cls, term: Term, value: Any) -> Criterion:
        return cls.compare_keys(term, value, "gt")

    @classmethod
    def greater_equal(cls, term: Term, value: Any) -> Criterion:
        return cls.compare_keys(term, value, "gte")

    @classmethod
    def less_than(cls, term: Term, value: Any) -> Criterion:
        return cls.compare_keys(term, value, "lt")

    @classmethod
    def less_equal(cls, term: Term, value: Any) -> Criterion:
        return cls.compare_keys(term, value, "lte")

    @classmethod
    def between(cls, term: Term, value: list[Any]) -> Criterion:
        """Between the two bounds - each a JSON text, a None given as the JSON null, which orders
        first: a None lower bound leaves the upper one alone, a None upper bound keeps no value (a JSON
        column keeps no null to be equal to it)."""
        lower, upper = value
        if lower is None:
            return Lookups.is_in(term, []) if upper is None else cls.less_equal(term, upper)
        if upper is None:
            # Only the JSON null orders at or below the JSON null - no value of a JSON column.
            return Lookups.is_in(term, [])
        return cls.greater_equal(term, lower) & cls.less_equal(term, upper)

    @classmethod
    def is_in(cls, term: Term, value: Any) -> Criterion:
        return Lookups.is_in(term, cls.get_value_texts(value))

    @classmethod
    def not_in(cls, term: Term, value: Any) -> Criterion:
        return Lookups.not_in(term, cls.get_value_texts(value))

    @classmethod
    def get_value_texts(cls, values: Any) -> Any:
        """The values of a list lookup as texts a path reads - a term (a subquery) as it is.

        Args:
            values: The JSON texts, or a term.

        Returns:
            The terms.
        """
        if not isinstance(values, (list, tuple, set)):
            return values
        return [None if value is None else cls.get_value_text(value) for value in values]
