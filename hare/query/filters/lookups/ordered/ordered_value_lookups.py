from __future__ import annotations

import dataclasses
import operator
from typing import TYPE_CHECKING, Any

from hare.query.filters.lookups.lookups import Lookups

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.query.filters.lookups.field_lookup import FieldLookup
    from hare.sql.terms.criteria.criterion import Criterion
    from hare.sql.terms.term import Term


class OrderedValueLookups:
    """The comparisons of a field whose values a database may order otherwise than PostgreSQL - ``gt``,
    ``gte``, ``lt``, ``lte`` and ``range`` as plain comparisons. A subclass names the values; a dialect
    ordering them otherwise replaces its operators in its filter operators."""

    @classmethod
    def greater_than(cls, term: Term, value: Any) -> Criterion:
        return operator.gt(term, value)

    @classmethod
    def greater_equal(cls, term: Term, value: Any) -> Criterion:
        return operator.ge(term, value)

    @classmethod
    def less_than(cls, term: Term, value: Any) -> Criterion:
        return operator.lt(term, value)

    @classmethod
    def less_equal(cls, term: Term, value: Any) -> Criterion:
        return operator.le(term, value)

    @classmethod
    def between(cls, term: Term, value: tuple[Any, Any]) -> Criterion:
        return Lookups.between(term, value)

    @classmethod
    def get_operators(cls) -> dict[str, Any]:
        """The comparison operators by the suffix of their lookup.

        Returns:
            The operators.
        """
        return {
            "gt": cls.greater_than,
            "gte": cls.greater_equal,
            "lt": cls.less_than,
            "lte": cls.less_equal,
            "range": cls.between,
        }

    @classmethod
    def with_comparisons(cls, lookups: dict[str, FieldLookup]) -> dict[str, FieldLookup]:
        """A field's lookups, its comparisons by these operators.

        Args:
            lookups: The field's lookups.

        Returns:
            The lookups.
        """
        for suffix, ordered_operator in cls.get_operators().items():
            if suffix in lookups:
                lookups[suffix] = dataclasses.replace(lookups[suffix], operator=ordered_operator)
        return lookups
