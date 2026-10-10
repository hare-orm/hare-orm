from __future__ import annotations

import operator
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, ClassVar

from hare.dialects.base.lookups.filter_operators import FilterOperators
from hare.dialects.clickhouse.lookups.clickhouse_json_path_lookups import ClickhouseJsonPathLookups
from hare.query.filters.lookups.json.json_path_lookups import JsonPathLookups
from hare.query.filters.lookups.lookups import Lookups

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.query.filters.lookups.field_lookup import FieldLookup


class ClickhouseFilterOperators(FilterOperators):
    """ClickHouse's operators - a list lookup of a JSON path value (``in``, ``not_in``) compares
    each value as the text the path reads, where the same lookup of any other value keeps its
    operator."""

    #: The JSON path list lookups' operators -> ClickHouse's.
    JSON_PATH_LIST_OPERATORS: ClassVar[dict[Callable[..., Any], Callable[..., Any]]] = {
        Lookups.is_in: ClickhouseJsonPathLookups.is_in,
        Lookups.not_in: ClickhouseJsonPathLookups.not_in,
    }

    #: The JSON path equality operators -> ClickHouse's, comparing the null of a JSON column.
    JSON_PATH_VALUE_OPERATORS: ClassVar[dict[Callable[..., Any], Callable[..., Any]]] = {
        operator.eq: ClickhouseJsonPathLookups.equal,
        Lookups.not_equal: ClickhouseJsonPathLookups.not_equal,
    }

    def get_overridden_operator(
        self, operator: Callable[..., Any], field_lookup: FieldLookup | None
    ) -> Callable[..., Any] | None:
        if field_lookup is not None and field_lookup.value_encoder == JsonPathLookups.encode_values:
            json_path_operator = self.JSON_PATH_LIST_OPERATORS.get(operator)
            if json_path_operator is not None:
                return json_path_operator
        if field_lookup is not None and field_lookup.value_encoder == JsonPathLookups.encode_value:
            json_path_operator = self.JSON_PATH_VALUE_OPERATORS.get(operator)
            if json_path_operator is not None:
                return json_path_operator
        return super().get_overridden_operator(operator, field_lookup)
