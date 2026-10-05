"""ClickHouse's own aggregates."""

from __future__ import annotations

from hare.dialects.clickhouse.functions.aggregates.declarations import (
    AnyLast,
    AnyValue,
    ArgMax,
    ArgMin,
    GroupUniqArray,
    SumMap,
    Uniq,
    UniqCombined,
    UniqExact,
)
from hare.dialects.clickhouse.functions.aggregates.group_array import GroupArray
from hare.dialects.clickhouse.functions.aggregates.median import Median
from hare.dialects.clickhouse.functions.aggregates.quantile import Quantile
from hare.dialects.clickhouse.functions.aggregates.quantiles import Quantiles
from hare.dialects.clickhouse.functions.aggregates.top_k import TopK

__all__ = [
    "AnyLast",
    "AnyValue",
    "ArgMax",
    "ArgMin",
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
