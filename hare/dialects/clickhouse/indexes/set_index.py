from __future__ import annotations

from typing import Any

from hare.dialects.clickhouse.indexes.clickhouse_index import ClickhouseIndex
from hare.dialects.clickhouse.indexes.constants import CLICKHOUSE_SET_INDEX_MAX_ROWS_RANGE


class SetIndex(ClickhouseIndex):
    """``TYPE set(max_rows)`` - the distinct values of each granule: skips the granules holding
    none of the values a filter looks for; for a key of few distinct values in a granule.

    Args:
        max_rows: The most distinct values kept of a granule - one holding more isn't skipped; 0 for
            no limit.

    Raises:
        ConfigurationError: ``max_rows`` isn't an int in its range.
    """

    INDEX_TYPE = "set"
    INTEGER_STORAGE_PARAMETERS = ("granularity", "max_rows")

    def __init__(self, *args: Any, max_rows: int = 0, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.max_rows = self.get_validated_integer("max_rows", max_rows, CLICKHOUSE_SET_INDEX_MAX_ROWS_RANGE)

    def get_type_arguments(self) -> tuple[Any, ...]:
        return (self.max_rows,)

    def deconstruct(self) -> tuple[str, list[Any], dict[str, Any]]:
        path, args, kwargs = super().deconstruct()
        if self.max_rows:
            kwargs["max_rows"] = self.max_rows
        return path, args, kwargs
