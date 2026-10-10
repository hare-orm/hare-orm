from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any, cast

from hare.dialects.base.renderers.checked_term_renderers import CheckedTermRenderers
from hare.dialects.clickhouse.renderers.clickhouse_aggregate_renderers import ClickhouseAggregateRenderers
from hare.dialects.clickhouse.renderers.clickhouse_cast_renderers import ClickhouseCastRenderers
from hare.dialects.clickhouse.renderers.clickhouse_condition_renderers import ClickhouseConditionRenderers
from hare.dialects.clickhouse.renderers.clickhouse_container_renderers import ClickhouseContainerRenderers
from hare.dialects.clickhouse.renderers.clickhouse_json_renderers import ClickhouseJsonRenderers
from hare.dialects.clickhouse.renderers.clickhouse_number_renderers import ClickhouseNumberRenderers
from hare.dialects.clickhouse.renderers.clickhouse_temporal_renderers import ClickhouseTemporalRenderers
from hare.dialects.clickhouse.renderers.clickhouse_text_renderers import ClickhouseTextRenderers
from hare.dialects.clickhouse.spatial.clickhouse_spatial_renderers import ClickhouseSpatialRenderers

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.clickhouse.clickhouse_dialect import ClickhouseDialect
    from hare.sql.terms.term import Term


class ClickhouseRenderers(CheckedTermRenderers):
    """How ClickHouse renders the terms whose SQL differs between dialects."""

    def add_own_renderers(self) -> None:
        ClickhouseAggregateRenderers.register(self)
        ClickhouseCastRenderers.register(self)
        ClickhouseConditionRenderers.register(self)
        ClickhouseContainerRenderers.register(self)
        ClickhouseJsonRenderers.register(self)
        ClickhouseTemporalRenderers.register(self)
        ClickhouseNumberRenderers.register(self)
        ClickhouseTextRenderers.register(self)
        ClickhouseSpatialRenderers.register(self)

    def get_json_path_comparand(
        self, value: Any, encode_json_text: Callable[[Any], str], column_type: str | None, *, as_parameter: bool
    ) -> Any:
        # A path reads JSON text as ClickHouse writes it: the value is compared as that text too -
        # an item of a list is turned into it by the list lookup's operator.
        # Local import: the lookups module imports hare's SQL terms, which render through this one.
        from hare.dialects.clickhouse.lookups.clickhouse_json_value_text import ClickhouseJsonValueText

        # Local import: the lookups module imports hare's SQL terms, which render through this one.
        from hare.dialects.clickhouse.lookups.declarations import ClickhouseJsonNullText
        from hare.sql.terms.values.value_wrapper import ValueWrapper

        if value is None and cast("ClickhouseDialect", self.dialect).stores_json_natively:
            # A JSON column keeps no null - a path holding one reads as a missing path, NULL.
            return None if as_parameter else ClickhouseJsonNullText(ValueWrapper(encode_json_text(value)))
        json_text = encode_json_text(value)
        return json_text if as_parameter else ClickhouseJsonValueText(ValueWrapper(json_text))

    def get_distinct_from_sql(self, left_sql: str, right_sql: str) -> str:
        # ClickHouse has IS DISTINCT FROM in no condition but a JOIN's: the two are distinct when they
        # aren't equal - a NULL on one side included - unless both are NULL.
        return f"(ifNull({left_sql} = {right_sql}, 0) = 0 AND NOT (isNull({left_sql}) AND isNull({right_sql})))"

    def get_concatenated_argument_sql(self, argument_sql: str, argument: Any) -> str:
        # concat() of text and a date, a UUID or another value that isn't text finds no common type.
        return f"toString({argument_sql})"

    def get_integer_aggregate_as_float(self, term: Term) -> Term:
        # Local import: hare.sql's terms render through the dialect. toFloat64() keeps the NULL of an
        # aggregate over no rows, where a CAST to Float64 would fail on it.
        from hare.sql.terms.functions.function import Function

        return Function("toFloat64", term)

    def get_never_null_column_count_argument(self, term: Term) -> Term:
        # Local import: hare.sql's terms render through the dialect. count() reads no column.
        from hare.sql.terms.star import Star

        return Star()

    def get_composite_distinct_key(self, terms: Sequence[Term]) -> Term:
        # Local import: hare.sql's terms render through the dialect.
        from hare.sql.terms.functions.function import Function

        return Function("tuple", *terms)
