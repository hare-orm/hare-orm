from __future__ import annotations

import datetime
import uuid
from collections.abc import Sequence
from typing import Any

from hare.dialects.base.parameters.large_in_list import LargeInList
from hare.dialects.clickhouse.client.declarations import ClickhouseValueSet
from hare.dialects.clickhouse.lookups.constants import CLICKHOUSE_VALUE_SET_INTEGER_TYPES, CLICKHOUSE_VALUE_SET_MINIMUM
from hare.dialects.clickhouse.lookups.in_list.clickhouse_value_set_values import ClickhouseValueSetValues
from hare.sql.terms.criteria.criterion import Criterion
from hare.sql.terms.term import Term


class ClickhouseLargeInList(LargeInList):
    """``__in``/``__not_in`` on ClickHouse for a list of at least ``CLICKHOUSE_VALUE_SET_MINIMUM`` values of
    one plain type each - UUIDs, integers, texts, dates - bound as one parameter a read sends as an
    external table; any other list keeps the plain ``IN (...)`` form."""

    @classmethod
    def get_membership_criterion(
        cls, term: Term, values: Sequence[Any], non_null_values: list[Any], element_type: str | None
    ) -> Criterion | None:
        value_set = cls.get_value_set(non_null_values)
        return None if value_set is None else term.isin(ClickhouseValueSetValues(value_set))

    @classmethod
    def get_row_container(
        cls, value_rows: list[tuple[Any, ...]], element_types: Sequence[str | None] | None = None
    ) -> Term | None:
        value_set = cls.get_row_value_set(value_rows)
        return None if value_set is None else ClickhouseValueSetValues(value_set)

    @classmethod
    def get_value_set(cls, values: list[Any]) -> ClickhouseValueSet | None:
        """The parameter of a list of single values.

        Args:
            values: The encoded values, None left out.

        Returns:
            The parameter - None for a short list, or values of no one plain type.
        """
        if len(values) < CLICKHOUSE_VALUE_SET_MINIMUM:
            return None
        column_type = cls.get_column_type(values)
        return None if column_type is None else ClickhouseValueSet((column_type,), list(values))

    @classmethod
    def get_row_value_set(cls, value_rows: list[tuple[Any, ...]]) -> ClickhouseValueSet | None:
        """The parameter of a list of rows of values.

        Args:
            value_rows: The rows, each holding one value per compared column.

        Returns:
            The parameter - None for few rows, or a column of values of no one plain type.
        """
        if len(value_rows) < CLICKHOUSE_VALUE_SET_MINIMUM or not value_rows[0]:
            return None
        column_types = []
        for position in range(len(value_rows[0])):
            column_type = cls.get_column_type([row[position] for row in value_rows])
            if column_type is None:
                return None
            column_types.append(column_type)
        return ClickhouseValueSet(tuple(column_types), [tuple(row) for row in value_rows])

    @staticmethod
    def get_column_type(values: list[Any]) -> str | None:
        """The type of the column holding values.

        Args:
            values: The values.

        Returns:
            The type - None for values of no one plain type (a None among them too).
        """
        value_type = type(values[0])
        for value in values:
            if type(value) is not value_type:
                return None
        if value_type is uuid.UUID:
            return "UUID"
        if value_type is str:
            return "String"
        if value_type is datetime.date:
            return "Date32"
        if value_type is int:
            least, greatest = min(values), max(values)
            for type_name, low, high in CLICKHOUSE_VALUE_SET_INTEGER_TYPES:
                if low <= least and greatest <= high:
                    return type_name
        return None
