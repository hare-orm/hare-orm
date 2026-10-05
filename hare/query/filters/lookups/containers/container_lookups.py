from __future__ import annotations

import operator
from functools import reduce
from typing import TYPE_CHECKING, Any

from hare.query.filters.lookups.lookups import Lookups
from hare.sql.terms.containers import (
    ArrayContainedByTerm,
    ArrayContainsTerm,
    ArrayElementTerm,
    ArrayLengthTerm,
    ArrayOverlapTerm,
    MapContainsKeyTerm,
)

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.fields.field import Field
    from hare.sql.terms.criteria.criterion import Criterion
    from hare.sql.terms.term import Term


class ContainerLookups:
    """The operators of container lookups - each builds a term the dialect writes."""

    @staticmethod
    def contains(term: Term, value: Term) -> Criterion:
        """The array holds every element of ``value``."""
        return ArrayContainsTerm(term, value)

    @staticmethod
    def contained_by(term: Term, value: Term) -> Criterion:
        """Every element of the array is in ``value``."""
        return ArrayContainedByTerm(term, value)

    @staticmethod
    def overlap(term: Term, value: Term) -> Criterion:
        """The array and ``value`` share an element."""
        return ArrayOverlapTerm(term, value)

    @staticmethod
    def length(term: Term, value: int) -> Criterion:
        """The array has ``value`` elements."""
        return ArrayLengthTerm(term).eq(value)

    @staticmethod
    def item(term: Term, value: tuple[int, Any], element_field: Field[Any] | None = None) -> Criterion:
        """``term[index] = comparison_value`` of the ``__item=(index, value)`` lookup - ``index`` is 0-based.

        Args:
            term: The array column.
            value: The ``(index, comparison_value)`` pair.
            element_field: The array's element term.

        Returns:
            The comparison criterion.
        """
        index, item_value = value
        return ArrayElementTerm(term, index, element_field=element_field).eq(item_value)

    @staticmethod
    def has_key(term: Term, value: Any) -> Criterion:
        """The map has the key ``value``."""
        return MapContainsKeyTerm(term, value)

    @staticmethod
    def has_keys(term: Term, value: list[Any]) -> Criterion:
        """The map has every key of ``value`` - any map for no key."""
        if not value:
            # 1=1 - true for every row.
            return Lookups.not_in(term, [])
        return reduce(operator.and_, (MapContainsKeyTerm(term, key) for key in value))

    @staticmethod
    def has_any_keys(term: Term, value: list[Any]) -> Criterion:
        """The map has a key of ``value`` - no map for no key."""
        if not value:
            # 1=0 - true for no row.
            return Lookups.is_in(term, [])
        return reduce(operator.or_, (MapContainsKeyTerm(term, key) for key in value))
