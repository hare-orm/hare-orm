from __future__ import annotations

from hare.dialects.clickhouse.indexes.clickhouse_index import ClickhouseIndex


class MinMaxIndex(ClickhouseIndex):
    """``TYPE minmax`` - the least and the greatest value of each granule: skips the granules a
    comparison or a range of the key can't match. A plain ``Index`` is one of granularity 1."""

    INDEX_TYPE = "minmax"
