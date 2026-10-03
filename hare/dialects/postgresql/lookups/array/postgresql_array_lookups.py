from __future__ import annotations

from typing import Any

from hare.dialects.postgresql.enums import PostgresqlArrayOperators
from hare.dialects.postgresql.fields.array import ArrayField
from hare.dialects.postgresql.functions.array.array_subscript import ArraySubscript
from hare.dialects.postgresql.lookups.array.array_length import ArrayLength
from hare.fields.base.field import Field
from hare.sql.terms.base.term import Term
from hare.sql.terms.criteria.basic_criterion import BasicCriterion
from hare.sql.terms.criteria.criterion import Criterion


class PostgresqlArrayLookups:
    """The PostgreSQL operators of array lookups."""

    @staticmethod
    def contains(field: Term, value: Term) -> Criterion:
        return BasicCriterion(PostgresqlArrayOperators.CONTAINS, field, value)

    @staticmethod
    def contained_by(field: Term, value: Term) -> Criterion:
        return BasicCriterion(PostgresqlArrayOperators.CONTAINED_BY, field, value)

    @staticmethod
    def overlap(field: Term, value: Term) -> Criterion:
        return BasicCriterion(PostgresqlArrayOperators.OVERLAP, field, value)

    @staticmethod
    def length(field: Term, value: int) -> Criterion:
        """Returns a criterion that checks if the array's first-dimension length equals the given value"""
        return ArrayLength.get_term(field).eq(value)

    @staticmethod
    def item(field: Term, value: tuple[int, Any], element_field: Field[Any] | None = None) -> Criterion:
        """``field[index] = comparison_value`` for the ``__item=(index, value)`` lookup - ``index`` is
        0-based.

        Args:
            field: The array column.
            value: The ``(index, comparison_value)`` pair.
            element_field: The array's element field - an array itself compares a whole row of a
                nested array.

        Returns:
            The comparison criterion.
        """
        index, item_value = value
        subarray_field = element_field if isinstance(element_field, ArrayField) else None
        return ArraySubscript(field, index, subarray_field=subarray_field).eq(item_value)
