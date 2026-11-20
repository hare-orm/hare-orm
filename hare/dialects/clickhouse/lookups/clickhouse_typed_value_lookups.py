from __future__ import annotations

import operator
from functools import partial
from typing import Any

from hare.dialects.clickhouse.lookups.constants import (
    CLICKHOUSE_DYNAMIC_COMPARED_DECIMAL_FUNCTION_NAME,
    CLICKHOUSE_DYNAMIC_COMPARED_DECIMAL_SCALE,
    CLICKHOUSE_DYNAMIC_DECIMAL_TYPE_PREFIX,
)
from hare.dialects.clickhouse.types.constants import CLICKHOUSE_DYNAMIC_INTEGER_TYPES
from hare.dialects.clickhouse.types.declarations import ClickhouseTypedValue
from hare.query.enums import Lookup
from hare.query.filters.lookups.field_lookup import FieldLookup
from hare.query.filters.lookups.lookups import Lookups
from hare.query.filters.lookups.value_encoders import ValueEncoders
from hare.sql.terms.criteria.criterion import Criterion
from hare.sql.terms.functions.function import Function
from hare.sql.terms.term import Term
from hare.sql.terms.values.value_wrapper import ValueWrapper


class ClickhouseTypedValueLookups:
    """The lookups of a column holding values of several types - a ``Dynamic`` or a ``Variant`` one: a
    value matches a held value of its own type only, compared as a value of that type; ``isnull``
    matches the NULL the column holds. The type is bound like the value - the SQL text is the same
    whatever type a value has. A column of any type (``Dynamic``) compares numbers by their family: an
    int with the held values of every integer type, a decimal with those of every decimal type - the
    type a number is kept as follows its size.

    Args:
        type_function_name: The function naming a held value's type.
        element_function_name: The function reading a held value as a value of a type.
        compares_number_families: Whether numbers are compared by their family.
    """

    def __init__(
        self, type_function_name: str, element_function_name: str, *, compares_number_families: bool = False
    ) -> None:
        self.type_function_name = type_function_name
        self.element_function_name = element_function_name
        self.compares_number_families = compares_number_families

    def get_lookups(self) -> dict[str, FieldLookup]:
        """The lookups by suffix.

        Returns:
            The lookups.
        """
        return {
            Lookup.EXACT: FieldLookup(partial(self.compare, comparison=operator.eq)),
            Lookup.NOT: FieldLookup(self.not_equal),
            Lookup.IN: FieldLookup(self.is_in, ValueEncoders.encode_list),
            Lookup.NOT_IN: FieldLookup(self.not_in, ValueEncoders.encode_list),
            Lookup.ISNULL: FieldLookup(Lookups.is_null, ValueEncoders.encode_bool),
            Lookup.NOT_ISNULL: FieldLookup(Lookups.not_null, ValueEncoders.encode_bool),
            Lookup.GT: FieldLookup(partial(self.compare, comparison=operator.gt)),
            Lookup.GTE: FieldLookup(partial(self.compare, comparison=operator.ge)),
            Lookup.LT: FieldLookup(partial(self.compare, comparison=operator.lt)),
            Lookup.LTE: FieldLookup(partial(self.compare, comparison=operator.le)),
            Lookup.RANGE: FieldLookup(self.between, ValueEncoders.encode_list),
        }

    def compare(self, term: Term, value: Any, comparison: Any) -> Criterion:
        """A held value of the value's type compared with it - a NULL value matches the NULL held, a term
        (``F()``) is compared as it is.

        Args:
            term: The column.
            value: The typed value, None or a term.
            comparison: The comparison - ``operator.eq``, ``operator.gt``, ...

        Returns:
            The condition.
        """
        if value is None or (isinstance(value, ClickhouseTypedValue) and value.value is None):
            return term.isnull()
        if isinstance(value, Term) or not isinstance(value, ClickhouseTypedValue):
            return comparison(term, value)
        if self.compares_number_families:
            if any(value.column_type == type_name for type_name, _, _ in CLICKHOUSE_DYNAMIC_INTEGER_TYPES):
                return self.get_integer_comparison(term, value, comparison)
            if value.column_type.startswith(CLICKHOUSE_DYNAMIC_DECIMAL_TYPE_PREFIX):
                return self.get_decimal_comparison(term, value, comparison)
        return self.get_held_comparison(term, ValueWrapper(value.column_type), value, comparison)

    def get_held_comparison(self, term: Term, held_type: Term, value: Any, comparison: Any) -> Criterion:
        """The held values of a type compared with a value.

        Args:
            term: The column.
            held_type: The type.
            value: The typed value.
            comparison: The comparison.

        Returns:
            The condition.
        """
        held_type_test = Function(self.type_function_name, term) == held_type
        element = Function(self.element_function_name, term, held_type)
        return held_type_test & comparison(element, ValueWrapper(value))

    def get_integer_comparison(self, term: Term, value: Any, comparison: Any) -> Criterion:
        """The held values of every integer type compared with an int.

        Args:
            term: The column.
            value: The typed int.
            comparison: The comparison.

        Returns:
            The condition.
        """
        return Criterion.any(
            self.get_held_comparison(term, ValueWrapper(type_name, allow_parametrize=False), value, comparison)
            for type_name, _, _ in CLICKHOUSE_DYNAMIC_INTEGER_TYPES
        )

    def get_decimal_comparison(self, term: Term, value: Any, comparison: Any) -> Criterion:
        """The held values of every decimal type compared with a decimal - each read as a decimal of
        ``CLICKHOUSE_DYNAMIC_COMPARED_DECIMAL_SCALE`` digits after the point.

        Args:
            term: The column.
            value: The typed decimal.
            comparison: The comparison.

        Returns:
            The condition.
        """
        decimal_type_test = Function(
            "startsWith",
            Function(self.type_function_name, term),
            ValueWrapper(CLICKHOUSE_DYNAMIC_DECIMAL_TYPE_PREFIX, allow_parametrize=False),
        )
        held_decimal = Function(
            CLICKHOUSE_DYNAMIC_COMPARED_DECIMAL_FUNCTION_NAME,
            Function("toString", term),
            ValueWrapper(CLICKHOUSE_DYNAMIC_COMPARED_DECIMAL_SCALE, allow_parametrize=False),
        )
        return (decimal_type_test == ValueWrapper(1, allow_parametrize=False)) & comparison(
            held_decimal, ValueWrapper(value)
        )

    def not_equal(self, term: Term, value: Any) -> Criterion:
        """A held value that isn't the value, including NULL unless the compared value is NULL.

        Args:
            term: The column.
            value: The typed value, None or a term.

        Returns:
            The condition.
        """
        if isinstance(value, Term):
            return Lookups.not_equal(term, value)
        return self.compare(term, value, operator.eq).is_not_true()

    def is_in(self, term: Term, value: Any) -> Criterion:
        """A held value equal to one of the values - a None matching the NULL held.

        Args:
            term: The column.
            value: The typed values, or a term (a subquery).

        Returns:
            The condition.
        """
        if isinstance(value, Term):
            return Lookups.is_in(term, value)
        if not value:
            return Lookups.is_in(term, [])
        return Criterion.any(self.compare(term, element, operator.eq) for element in value)

    def not_in(self, term: Term, value: Any) -> Criterion:
        """A held value equal to none of the values; NULL matches unless included in the list.

        Args:
            term: The column.
            value: The typed values, or a term (a subquery).

        Returns:
            The condition.
        """
        if isinstance(value, Term):
            return Lookups.not_in(term, value)
        return self.is_in(term, value).is_not_true()

    def between(self, term: Term, value: list[Any]) -> Criterion:
        """A held value from the lower bound to the upper one - of the bounds' type.

        Args:
            term: The column.
            value: The two typed bounds - a None bound matches nothing.

        Returns:
            The condition.
        """
        lower, upper = value
        if lower is None or upper is None:
            return Lookups.is_in(term, [])
        return self.compare(term, lower, operator.ge) & self.compare(term, upper, operator.le)
