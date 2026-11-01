"""ClickHouse's own functions."""

from __future__ import annotations

from hare.dialects.clickhouse.clickhouse_dialect import ClickhouseDialect
from hare.dialects.clickhouse.functions.aggregates import (
    AnyLast,
    AnyValue,
    ArgMax,
    ArgMin,
    GroupArray,
    GroupUniqArray,
    Median,
    Quantile,
    Quantiles,
    SumMap,
    TopK,
    Uniq,
    UniqCombined,
    UniqExact,
)
from hare.dialects.clickhouse.functions.dict_get import DictGet
from hare.query.expressions.dialect_expressions import DialectExpressions

__all__ = [
    "AnyLast",
    "AnyValue",
    "ArgMax",
    "ArgMin",
    "DictGet",
    "GroupArray",
    "GroupUniqArray",
    "Median",
    "Quantile",
    "Quantiles",
    "SumMap",
    "TopK",
    "Uniq",
    "UniqCombined",
    "UniqExact",
]

# The functions only ClickHouse has - a query on another database refuses each before any SQL.
DialectExpressions.register(
    ClickhouseDialect,
    (
        AnyLast,
        AnyValue,
        ArgMax,
        ArgMin,
        DictGet,
        GroupArray,
        GroupUniqArray,
        Median,
        Quantile,
        Quantiles,
        SumMap,
        TopK,
        Uniq,
        UniqCombined,
        UniqExact,
    ),
)
