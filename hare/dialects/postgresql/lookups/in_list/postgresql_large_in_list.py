from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from hare.dialects.base.parameters.large_in_list import LargeInList
from hare.dialects.postgresql.functions.array.any_value import AnyValue
from hare.dialects.postgresql.lookups.in_list.array_parameter import ArrayParameter
from hare.dialects.postgresql.lookups.in_list.unnest_row_values import UnnestRowValues
from hare.dialects.postgresql.parameters.constants import POSTGRES_IN_ARRAY_THRESHOLD
from hare.sql.enums import Equality
from hare.sql.terms.criteria.basic_criterion import BasicCriterion
from hare.sql.terms.criteria.criterion import Criterion
from hare.sql.terms.term import Term


class PostgresqlLargeInList(LargeInList):
    """``__in``/``__not_in`` on Postgres for a value list too long to bind one parameter per value -
    bound as one array parameter (``= ANY($1::type[])``, or ``unnest`` for value rows), typed by
    the compared field or, when none is known, by the values themselves."""

    @classmethod
    def get_array_term(cls, values: list[Any], array_element_type: str, *, typed_by_values: bool = False) -> Term:
        """One array parameter holding ``values``, cast to ``array_element_type[]``.

        Args:
            values: The array elements.
            array_element_type: The element SQL type.
            typed_by_values: Whether the element type was taken from the values.

        Returns:
            The cast array term.
        """
        return ArrayParameter(values, array_element_type, typed_by_values=typed_by_values)

    @classmethod
    def get_membership_criterion(
        cls, term: Term, values: Sequence[Any], non_null_values: list[Any], element_type: str | None
    ) -> Criterion | None:
        # A SQL term element keeps the plain IN (...) form.
        if len(values) < POSTGRES_IN_ARRAY_THRESHOLD or ArrayParameter.holds_term(values):
            return None
        typed_by_values = element_type is None
        element_type = element_type or ArrayParameter.get_array_element_type(non_null_values)
        if element_type is None:
            return None
        array_term = cls.get_array_term(non_null_values, element_type, typed_by_values=typed_by_values)
        return BasicCriterion(Equality.EQ, term, AnyValue(array_term))

    @classmethod
    def get_row_container(
        cls, value_rows: list[tuple[Any, ...]], element_types: Sequence[str | None] | None
    ) -> UnnestRowValues | None:
        """The ``unnest`` container for a long list of value rows.

        Args:
            value_rows: The value rows, each holding one value per compared column.
            element_types: Per column, its SQL type, or None to type it by its values.

        Returns:
            The container, or None when the plain row list is kept.
        """
        if len(value_rows) < POSTGRES_IN_ARRAY_THRESHOLD:
            return None
        column_count = len(value_rows[0])
        known_element_types = element_types or (None,) * column_count
        column_arrays = []
        for index, known_element_type in enumerate(known_element_types):
            column_values = [row[index] for row in value_rows]
            if ArrayParameter.holds_term(column_values):
                return None
            element_type = known_element_type or ArrayParameter.get_array_element_type(
                [column_value for column_value in column_values if column_value is not None]
            )
            if element_type is None:
                return None
            column_arrays.append(cls.get_array_term(column_values, element_type))
        return UnnestRowValues(column_arrays)
