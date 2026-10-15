from __future__ import annotations

import abc
from typing import TYPE_CHECKING, Any

from hare.exceptions import QueryError

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.fields.field import Field
    from hare.query.filters.lookups.ordered.ordered_value_lookups import OrderedValueLookups
    from hare.sql.terms.criteria.criterion import Criterion
    from hare.sql.terms.term import Term


class ClickhouseOrderKeyLookups(abc.ABC):
    """Comparisons of values ClickHouse orders otherwise than PostgreSQL - each side compared by a key
    ordering them as PostgreSQL does. A subclass gives the key of its values."""

    @staticmethod
    @abc.abstractmethod
    def get_order_key(term: Term) -> Term:
        """The key a column orders by.

        Args:
            term: The column.

        Returns:
            The key.
        """

    @classmethod
    def get_field_order_key(cls, field: Field[Any], term: Term) -> Term:
        """The key a column of the field orders by - the ``ordering_key`` of its type mapping.

        Args:
            field: The column's field.
            term: The column.

        Returns:
            The key.
        """
        return cls.get_order_key(term)

    @staticmethod
    @abc.abstractmethod
    def get_value_order_key(value: Any) -> Term:
        """The key a compared value orders by.

        Args:
            value: The value, or a term holding it.

        Returns:
            The key.
        """

    @classmethod
    def greater_than(cls, term: Term, value: Any) -> Criterion:
        return cls.get_order_key(term) > cls.get_value_order_key(value)

    @classmethod
    def greater_equal(cls, term: Term, value: Any) -> Criterion:
        return cls.get_order_key(term) >= cls.get_value_order_key(value)

    @classmethod
    def less_than(cls, term: Term, value: Any) -> Criterion:
        return cls.get_order_key(term) < cls.get_value_order_key(value)

    @classmethod
    def less_equal(cls, term: Term, value: Any) -> Criterion:
        return cls.get_order_key(term) <= cls.get_value_order_key(value)

    @classmethod
    def between(cls, term: Term, value: tuple[Any, Any]) -> Criterion:
        """Between the two bounds - a None bound leaves that end open, as ``__range`` does."""
        if len(value) != 2:
            raise QueryError(f"__range expects exactly 2 values (lower, upper), got {len(value)}: {value!r}")
        lower, upper = value
        if lower is None and upper is None:
            return term.notnull()
        if lower is None:
            return cls.less_equal(term, upper)
        if upper is None:
            return cls.greater_equal(term, lower)
        return cls.greater_equal(term, lower) & cls.less_equal(term, upper)

    @classmethod
    def get_overrides(cls, generic_lookups: type[OrderedValueLookups]) -> dict[Any, Any]:
        """The filter operators replacing a field's comparisons.

        Args:
            generic_lookups: The comparisons of the field.

        Returns:
            Its operators -> these.
        """
        order_key_operators = {
            "gt": cls.greater_than,
            "gte": cls.greater_equal,
            "lt": cls.less_than,
            "lte": cls.less_equal,
            "range": cls.between,
        }
        return {
            generic_operator: order_key_operators[suffix]
            for suffix, generic_operator in generic_lookups.get_operators().items()
        }
